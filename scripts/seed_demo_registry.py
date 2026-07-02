"""Bake a **demo** MLflow registry for the hosted free-tier deploy (F6).

The serving app (F4) answers ``/predict`` and reports ``model_loaded=true`` only when a
version is promoted to the ``production`` alias. A fresh cloud deploy starts with an
**empty** registry, so a live ``/health`` would honestly say ``model_loaded=false`` and a
reviewer clicking the link would see no model. This script produces a small, portable
registry with one trained-and-promoted model so the deployed image serves a real
prediction the moment it comes up.

**Honest-status boundary (ADR-001 stays intact).** This trains on the committed *smoke
fixture*, never the full dataset — because the deployed image must build offline on a free
runner with no generator and no network. That is allowed precisely because this model is a
**demo for the live endpoint, not a reported metric**: ADR-001 forbids *reporting* a model
scored on the reduced fixture, not *serving* one to prove the endpoint is wired. Every
surface says so — the run is tagged ``demo=fixture``, ``/model-info`` exposes the fixture
provenance, and the README's live-link note calls it a demo model. The full-data model with
its real ≈0.82 ROC-AUC is what ``pdm train`` produces locally (STATE.md), and that is the
one whose number is ever quoted.

**Portability.** MLflow stores artifact locations as absolute paths in the tracking DB, so
the store is only relocatable if the build path equals the run path. The Docker image builds
and serves from the *same* fixed directory (``/mlflow`` — ``--store-dir``), so the baked DB's
absolute artifact URIs resolve at run time. The DB and the artifacts are colocated under one
directory (an explicit experiment ``artifact_location``, since MLflow otherwise scatters
artifacts to a CWD-relative ``mlruns/``), so the whole store is one self-contained tree.

Usage (run from the repo root, after ``pip install -e '.[serve]'``)::

    python scripts/seed_demo_registry.py --store-dir /mlflow

Deterministic: a fixed class-rich seed → the same demo model every build.
"""

from __future__ import annotations

import argparse
import warnings
from pathlib import Path

import mlflow
from mlflow.tracking import MlflowClient

from pdm_mlops import config, registry, train as train_mod

#: A **class-rich** fixture seed. The default training seed (42) lands a single-class
#: unit-grouped test split on the reduced fixture (``DegenerateSplit``); 0 is class-rich
#: on both sides, so the fixture train scores a real ROC-AUC. This only ever touches the
#: fixture — the full dataset is class-rich at any seed (ADR-003).
DEMO_SEED: int = 0

#: Marks every run and the registered version as a fixture-trained demo, so no consumer
#: can mistake the served model for a full-data reported one.
DEMO_TAG: dict[str, str] = {"demo": "fixture", "provenance": "smoke-fixture (not a reported metric)"}


def seed_registry(store_dir: Path, *, seed: int = DEMO_SEED) -> str:
    """Train + promote a demo model into a self-contained MLflow store at ``store_dir``.

    Returns the promoted production version (a string). Idempotent enough for a rebuild:
    a fresh ``store_dir`` yields version ``1``; re-running against an existing store adds a
    version and promotes it through the same F3 gate.
    """
    store_dir.mkdir(parents=True, exist_ok=True)
    db = store_dir / "mlflow.db"
    tracking_uri = config.sqlite_tracking_uri(db)
    mlflow.set_tracking_uri(tracking_uri)

    client = MlflowClient(tracking_uri=tracking_uri)
    # Colocate artifacts with the DB (MLflow otherwise writes them to a CWD-relative
    # ``mlruns/`` that would not travel with the image). Create the experiment with an
    # explicit artifact_location the first time; reuse it on a rebuild.
    if client.get_experiment_by_name(config.EXPERIMENT_NAME) is None:
        artifact_location = (store_dir / "artifacts").resolve().as_uri()
        client.create_experiment(config.EXPERIMENT_NAME, artifact_location=artifact_location)

    # Train on the fixture (never the full dataset — this build is offline by design) and
    # register the winner. The fixture guarantees a class-rich split at DEMO_SEED.
    readings = features_fixture()
    summary = train_mod.train(
        seed=seed, tracking_uri=tracking_uri, readings=readings, register=True
    )
    # Normalise to str at the boundary, matching the registry layer's convention
    # (MLflow returns int on some paths, str on others; `registry.production_version`
    # hands back str). Keeping the same type makes the returned version compare equal.
    version = str(summary.registered_version)
    assert summary.registered_version is not None, "register=True must yield a version"

    # Tag the run and the version as a demo so provenance is unmissable downstream.
    client.set_model_version_tag(config.REGISTERED_MODEL_NAME, version, "demo", DEMO_TAG["demo"])
    client.set_model_version_tag(
        config.REGISTERED_MODEL_NAME, version, "provenance", DEMO_TAG["provenance"]
    )

    # Promote through the *real* F3 gate (first promotion always passes — nothing to
    # protect yet), so the served model went through the same governed path as any other.
    reg_client = registry._client(tracking_uri)
    result = registry.promote(
        reg_client, config.REGISTERED_MODEL_NAME, version, gate=True
    )
    assert result.promoted, f"demo promotion unexpectedly held: {result}"
    return version


def features_fixture():
    """Load the committed smoke fixture as the demo training frame."""
    import pandas as pd

    return pd.read_parquet(config.SAMPLE_READINGS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--store-dir",
        type=Path,
        required=True,
        help="directory for the self-contained MLflow store (DB + artifacts). "
        "In Docker this MUST equal the run-time path (absolute artifact URIs are baked).",
    )
    parser.add_argument("--seed", type=int, default=DEMO_SEED, help="class-rich fixture seed")
    args = parser.parse_args()

    # The fixture-fallback UserWarning is expected here (we deliberately train on it).
    warnings.simplefilter("ignore")
    version = seed_registry(args.store_dir.resolve(), seed=args.seed)
    print(
        f"Baked demo registry at {args.store_dir} — "
        f"'{config.REGISTERED_MODEL_NAME}' v{version} promoted to "
        f"'{registry.PRODUCTION_ALIAS}' (fixture-trained demo model)."
    )


if __name__ == "__main__":
    main()
