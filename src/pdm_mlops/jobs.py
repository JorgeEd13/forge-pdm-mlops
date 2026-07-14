"""Kicking off the generation worker — the web/worker boundary itself (F14a).

**This module is the whole of decision S2** (ADR-026). Generation runs in its own
deployable unit, so the serving process's entire job is to *enqueue*: write a ``queued``
run, ask something else to execute it, and answer 202. It never generates.

The temptation this exists to refuse is `BackgroundTasks`. It is three lines, it "works",
and it quietly collapses the system back into **one container** — which would make the
Kubernetes phase (F16) resume-driven box-ticking over a single process, and leave Terraform
(F17) one resource to codify. It is also wrong on its own terms: a Cloud Run instance that
scales to zero can be shut down mid-task, so a background thread's work simply vanishes,
and a runaway generation competes for the free container's single CPU with the requests it
is supposed to be serving.

Two triggers, one interface:

* :class:`CloudRunJobTrigger` — **the deployed topology.** Calls the Cloud Run Admin API to
  start an execution of a *Cloud Run Job* with the run's parameters as env overrides. The
  job is a separate service with its own image, lifecycle and scaling, and it is free-tier
  eligible. Authenticates with the instance's own service-account token from the metadata
  server, over stdlib ``urllib`` — no new dependency for one HTTP POST.
* :class:`LocalProcessTrigger` — **a development stand-in**, for `pdm serve` on a laptop
  where there is no Cloud Run. It spawns the same worker entry point as a detached OS
  process. Note honestly what it is and is not: the API process still never runs the forge
  (the S2 invariant holds, and the tests assert it), but a subprocess on the same host is
  *not* a separate deployable unit. The deployed system uses the Cloud Run Job; the demo
  page says which trigger is live.

When neither is configured, :func:`open_trigger` returns ``None`` and the feature is
honestly unavailable — the same graceful-degrade contract as the store.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Protocol

from . import generate

#: Env vars that select and configure the trigger (set by the deploy).
CLOUD_RUN_JOB_ENV = "GENERATION_JOB"          # the Cloud Run Job name
CLOUD_RUN_PROJECT_ENV = "GENERATION_PROJECT"  # GCP project id
CLOUD_RUN_REGION_ENV = "GENERATION_REGION"    # e.g. us-central1
LOCAL_WORKER_ENV = "GENERATION_LOCAL_WORKER"  # "1" → the dev subprocess stand-in

_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/token"
)


class TriggerError(RuntimeError):
    """The worker could not be started — the run stays visible and is marked failed."""


class JobTrigger(Protocol):
    """Start the generation worker for an already-``queued`` run."""

    #: Short, honest name of the topology in use — surfaced to the user.
    name: str

    def trigger(self, run_id: str, spec: generate.GenerationSpec) -> None: ...


class CloudRunJobTrigger:
    """Start a **Cloud Run Job** execution — the deployed web/worker split (S2).

    The job's container is the worker image; the run's parameters ride in as env
    overrides, so one job definition serves every generation request and nothing about a
    request is baked into the deploy.
    """

    name = "cloudrun-job"

    def __init__(self, project: str, region: str, job: str, *, timeout: float = 20.0) -> None:
        self.project = project
        self.region = region
        self.job = job
        self.timeout = timeout

    @property
    def _url(self) -> str:
        return (
            f"https://run.googleapis.com/v2/projects/{self.project}"
            f"/locations/{self.region}/jobs/{self.job}:run"
        )

    def _access_token(self) -> str:
        """The instance service account's token, from Cloud Run's metadata server."""
        req = urllib.request.Request(
            _METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())["access_token"]
        except Exception as exc:  # noqa: BLE001
            raise TriggerError(
                "could not obtain a service-account token from the metadata server "
                "(is this running on Cloud Run?)"
            ) from exc

    def trigger(self, run_id: str, spec: generate.GenerationSpec) -> None:
        body = {
            "overrides": {
                "containerOverrides": [
                    {
                        "env": [
                            {"name": "RUN_ID", "value": run_id},
                            {"name": "GENERATION_UNITS", "value": str(spec.n_units)},
                            {"name": "GENERATION_DAYS", "value": str(spec.days)},
                            {"name": "GENERATION_SEED", "value": str(spec.seed)},
                        ]
                    }
                ]
            }
        }
        req = urllib.request.Request(
            self._url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status not in (200, 201):
                    raise TriggerError(f"Cloud Run Jobs API returned HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            raise TriggerError(
                f"could not start job {self.job!r}: HTTP {exc.code} {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise TriggerError(f"could not reach the Cloud Run Jobs API: {exc.reason}") from exc


class LocalProcessTrigger:
    """Run the worker as a detached local process — **development only** (see module docs).

    The API process still does not generate: it spawns the very same ``pdm generate-run``
    entry point the Cloud Run Job runs. But this is one host, not two deployable units —
    do not read a local demo as evidence for the S2 topology.
    """

    name = "local-subprocess"

    def trigger(self, run_id: str, spec: generate.GenerationSpec) -> None:
        cmd = [
            sys.executable, "-m", "pdm_mlops.cli", "generate-run",
            "--run-id", run_id,
            "--units", str(spec.n_units),
            "--days", str(spec.days),
            "--seed", str(spec.seed),
        ]
        try:
            subprocess.Popen(  # noqa: S603 — fixed argv, values are ints/uuid from a validated spec
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise TriggerError(f"could not spawn the local worker: {exc}") from exc


def open_trigger() -> JobTrigger | None:
    """Select the trigger from the environment, or ``None`` when generation is off.

    Cloud Run Job wins when configured (the deployed path); the local stand-in must be
    asked for explicitly, so a misconfigured cloud deploy can never *silently* fall back to
    generating on the serving container — it fails honestly instead.
    """
    job = os.environ.get(CLOUD_RUN_JOB_ENV)
    project = os.environ.get(CLOUD_RUN_PROJECT_ENV)
    region = os.environ.get(CLOUD_RUN_REGION_ENV)
    if job and project and region:
        return CloudRunJobTrigger(project=project, region=region, job=job)
    if os.environ.get(LOCAL_WORKER_ENV) == "1":
        return LocalProcessTrigger()
    return None
