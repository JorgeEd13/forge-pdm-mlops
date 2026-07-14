"""F14a — the generate endpoints and the web/worker boundary (ADR-026).

Drives ``POST /demo/generate`` → poll → browse → report through a ``TestClient`` against a
tmp SQLite store and a fixture-trained model in a tmp MLflow backend, with a **fake
trigger** standing in for the Cloud Run Job. The fake is the point: it lets a test observe
what the API does at the boundary — enqueue and hand off — and, crucially, what it does
*not* do.

Needs ``[serve]`` + ``[cloud]``. The forge (``[generate]``) is never needed here: the API
must not be able to run it, which is exactly what these tests assert.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from mlflow.tracking import MlflowClient

pytest.importorskip("fastapi", reason="needs the `[serve]` extra (F4/ADR-009)")
pytest.importorskip("httpx", reason="needs the `[serve]` extra (F4/ADR-009)")
pytest.importorskip("sqlalchemy", reason="needs the `[cloud]` extra (F7/ADR-015)")

from fastapi.testclient import TestClient  # noqa: E402

from pdm_mlops import (  # noqa: E402
    config,
    features,
    generate,
    jobs,
    registry,
    serve,
    store_gen,
    train,
    worker,
)

NAME = config.REGISTERED_MODEL_NAME


class FakeTrigger:
    """Records the hand-off. Runs nothing — like the real one, from the API's point of view."""

    name = "fake"

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, generate.GenerationSpec]] = []
        self.fail = fail

    def trigger(self, run_id: str, spec: generate.GenerationSpec) -> None:
        if self.fail:
            raise jobs.TriggerError("no worker configured")
        self.calls.append((run_id, spec))


@pytest.fixture
def tmp_tracking(tmp_path):
    return config.sqlite_tracking_uri(tmp_path / "mlflow.db")


@pytest.fixture
def fixture_readings() -> pd.DataFrame:
    return pd.read_parquet(config.SAMPLE_READINGS)


@pytest.fixture
def gen_store(tmp_path):
    s = store_gen.open_store(f"sqlite:///{(tmp_path / 'gen.db').as_posix()}")
    yield s
    s.dispose()


def _promote(tmp_tracking: str, readings: pd.DataFrame) -> str:
    summary = train.train(seed=0, tracking_uri=tmp_tracking, readings=readings, register=True)
    client = MlflowClient(tracking_uri=tmp_tracking, registry_uri=tmp_tracking)
    assert registry.promote(client, NAME, summary.registered_version).promoted
    return str(summary.registered_version)


def _client(tmp_tracking, gen_store, trigger) -> TestClient:
    return TestClient(
        serve.create_app(
            store=serve.ModelStore(tracking_uri=tmp_tracking),
            generation_store=gen_store,
            job_trigger=trigger,
        )
    )


def _synthetic_fleet(n_units: int = 4, n_rows: int = 30) -> pd.DataFrame:
    """Stands in for the forge's output — the API/worker split doesn't care who made it."""
    rng = np.random.default_rng(0)
    frame = pd.DataFrame(
        {
            "unit_id": np.repeat([f"u{i}" for i in range(n_units)], n_rows),
            "t_index": list(range(n_rows)) * n_units,
        }
    )
    for i, signal in enumerate(features.FEATURE_COLUMNS):
        frame[signal] = rng.normal(100 * (i + 1), 5, len(frame))
    return frame


# --- THE decision this phase exists to make honest (S2) -----------------------


def test_the_api_never_generates_in_process(tmp_tracking, gen_store, monkeypatch) -> None:
    """Decision S2: generation runs in a **separate deployable unit**, never in the API.

    This is the test that has to fail if someone reaches for ``BackgroundTasks`` (or a
    thread, or a direct call) because it is three lines and it "works". The API's whole job
    at kick-off is: write a queued run, hand it to the worker, answer 202.
    """
    def must_not_run(spec):  # pragma: no cover - the point is that it never executes
        raise AssertionError(
            "the API process ran the forge — that collapses the web/worker split "
            "(ADR-026 / S2) and hollows out the K8s and Terraform phases."
        )

    monkeypatch.setattr(generate, "run_generation", must_not_run)
    trigger = FakeTrigger()
    client = _client(tmp_tracking, gen_store, trigger)

    resp = client.post("/demo/generate", json={"n_units": 5, "days": 2})

    assert resp.status_code == 202                       # accepted, not "done"
    body = resp.json()
    assert body["status"] == store_gen.STATUS_QUEUED     # nothing generated yet
    assert body["n_rows"] == 0
    assert body["worker"] == "fake"                      # it was handed OFF
    assert trigger.calls == [(body["run_id"], generate.GenerationSpec(n_units=5, days=2))]
    # The run is durable state, not in-process state — it survives this container.
    assert gen_store.get_run(body["run_id"]).status == store_gen.STATUS_QUEUED


# --- kick off -----------------------------------------------------------------


def test_a_capped_request_is_a_400_naming_the_cap(tmp_tracking, gen_store) -> None:
    client = _client(tmp_tracking, gen_store, FakeTrigger())
    resp = client.post(
        "/demo/generate", json={"n_units": generate.MAX_UNITS, "days": generate.MAX_DAYS}
    )
    assert resp.status_code == 400                       # a 4xx, never a wedged container
    assert "unit-days" in resp.json()["detail"]


def test_generation_is_honestly_unavailable_without_a_store_or_worker(tmp_tracking) -> None:
    """Graceful degrade: no DB / no worker → 503 that says so; the rest of the demo is fine."""
    client = TestClient(
        serve.create_app(
            store=serve.ModelStore(tracking_uri=tmp_tracking),
            generation_store=None,
            job_trigger=None,
        )
    )
    resp = client.post("/demo/generate", json={"n_units": 5, "days": 2})
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"]
    assert client.get("/health").status_code == 200      # …and nothing else broke


def test_a_worker_that_cannot_start_is_recorded_on_the_run(tmp_tracking, gen_store) -> None:
    client = _client(tmp_tracking, gen_store, FakeTrigger(fail=True))
    resp = client.post("/demo/generate", json={"n_units": 5, "days": 2})

    assert resp.status_code == 503
    # The run row exists and says why — the failure is where the user is looking.
    runs = [r for r in [gen_store.get_run(rid) for rid in _all_run_ids(gen_store)] if r]
    assert len(runs) == 1 and runs[0].status == store_gen.STATUS_FAILED
    assert "no worker configured" in runs[0].error


def _all_run_ids(store: store_gen.GenerationStore) -> list[str]:
    import sqlalchemy as sa

    with store._engine.connect() as conn:  # noqa: SLF001 — a test may look inside
        return [r[0] for r in conn.execute(sa.text("select run_id from generation_runs"))]


# --- poll, browse, report -----------------------------------------------------


def test_poll_then_browse_then_report(tmp_tracking, gen_store, fixture_readings, monkeypatch) -> None:
    """The F14a definition of done, end to end: kick off → poll → browse → risk roll-up."""
    _promote(tmp_tracking, fixture_readings)
    trigger = FakeTrigger()
    client = _client(tmp_tracking, gen_store, trigger)

    run_id = client.post("/demo/generate", json={"n_units": 4, "days": 1}).json()["run_id"]
    assert client.get(f"/demo/generate/{run_id}").json()["status"] == store_gen.STATUS_QUEUED

    # The worker runs out-of-process; here we run it directly, as the Cloud Run Job would.
    monkeypatch.setattr(generate, "run_generation", lambda spec: _synthetic_fleet())
    worker.execute_run(run_id, generate.GenerationSpec(n_units=4, days=1), gen_store)

    polled = client.get(f"/demo/generate/{run_id}").json()
    assert polled["status"] == store_gen.STATUS_SUCCEEDED
    assert polled["n_rows"] == 120

    page = client.get(f"/demo/generate/{run_id}/rows", params={"offset": 0, "limit": 10}).json()
    assert page["total_rows"] == 120 and len(page["rows"]) == 10
    assert set(features.FEATURE_COLUMNS) <= set(page["rows"][0])
    assert page["columns"] == generate.stored_columns()

    report = client.get(f"/demo/generate/{run_id}/report").json()
    assert report["n_units"] == 4
    assert report["n_rows_scored"] == 120
    assert report["flag_threshold"] == generate.FLAG_THRESHOLD
    assert "rolling mean" in report["rollup_rule"]
    # Ranked riskiest-first, and every vehicle carries the raw numbers the rule *doesn't*
    # rank on (peak, share) so the report can show its work.
    risks = [u["risk"] for u in report["units"]]
    assert risks == sorted(risks, reverse=True)
    assert all({"peak", "high_risk_share", "n_rows"} <= set(u) for u in report["units"])
    assert report["n_flagged"] == sum(1 for u in report["units"] if u["flagged"])
    # `demo` is tag-driven, like the upload response: only the F6 seed script tags a
    # version `demo=fixture`. This locally-trained one is untagged, so the roll-up honestly
    # reports demo:false — the banner follows the model, not the endpoint.
    assert report["demo"] is False


def test_the_demo_fixture_banner_holds_on_the_roll_up(
    tmp_tracking, gen_store, fixture_readings, monkeypatch
) -> None:
    """The hosted deploy serves the fixture-trained DEMO model (ADR-014) — the report says so.

    The roll-up is the newest surface that puts a number in front of a stranger, so it
    carries the same honesty flag as ``/model-info`` and the F8 upload: when the scoring
    model is tagged ``demo=fixture``, the report is labelled a demo.
    """
    version = _promote(tmp_tracking, fixture_readings)
    MlflowClient(tracking_uri=tmp_tracking, registry_uri=tmp_tracking).set_model_version_tag(
        NAME, version, "demo", "fixture"      # what scripts/seed_demo_registry.py does
    )
    client = _client(tmp_tracking, gen_store, FakeTrigger())
    run_id = client.post("/demo/generate", json={"n_units": 4, "days": 1}).json()["run_id"]
    monkeypatch.setattr(generate, "run_generation", lambda spec: _synthetic_fleet())
    worker.execute_run(run_id, generate.GenerationSpec(n_units=4, days=1), gen_store)

    assert client.get(f"/demo/generate/{run_id}/report").json()["demo"] is True


def test_report_is_409_until_the_run_succeeds(tmp_tracking, gen_store, fixture_readings) -> None:
    _promote(tmp_tracking, fixture_readings)
    client = _client(tmp_tracking, gen_store, FakeTrigger())
    run_id = client.post("/demo/generate", json={"n_units": 4, "days": 1}).json()["run_id"]

    resp = client.get(f"/demo/generate/{run_id}/report")
    assert resp.status_code == 409                       # queued — nothing to report on yet
    assert store_gen.STATUS_QUEUED in resp.json()["detail"]


def test_a_failed_runs_reason_reaches_the_report(tmp_tracking, gen_store, fixture_readings) -> None:
    _promote(tmp_tracking, fixture_readings)
    client = _client(tmp_tracking, gen_store, FakeTrigger())
    run_id = client.post("/demo/generate", json={"n_units": 4, "days": 1}).json()["run_id"]
    gen_store.mark_failed(run_id, error="the forge exploded")

    resp = client.get(f"/demo/generate/{run_id}/report")
    assert resp.status_code == 409 and "the forge exploded" in resp.json()["detail"]


def test_unknown_run_is_a_404(tmp_tracking, gen_store) -> None:
    client = _client(tmp_tracking, gen_store, FakeTrigger())
    assert client.get("/demo/generate/nope").status_code == 404
    assert client.get("/demo/generate/nope/rows").status_code == 404
    assert client.get("/demo/generate/nope/report").status_code == 404


def test_report_is_503_without_a_promoted_model(tmp_tracking, gen_store, monkeypatch) -> None:
    client = _client(tmp_tracking, gen_store, FakeTrigger())
    run_id = client.post("/demo/generate", json={"n_units": 4, "days": 1}).json()["run_id"]
    monkeypatch.setattr(generate, "run_generation", lambda spec: _synthetic_fleet())
    worker.execute_run(run_id, generate.GenerationSpec(n_units=4, days=1), gen_store)

    # Data is there, but nothing is promoted: 503 (the process is healthy, it has no model).
    assert client.get(f"/demo/generate/{run_id}/report").status_code == 503


def test_the_report_follows_the_promoted_model_not_a_baked_score(
    tmp_tracking, gen_store, fixture_readings, monkeypatch
) -> None:
    """The registry's whole property, upheld on the roll-up: promote → the report re-scores.

    The score is not frozen into the store at generation time. It is cached per model
    version, so a promotion (or a rollback) changes what the report says with no redeploy —
    the same guarantee ``/predict`` gives (ADR-008/ADR-009).
    """
    v1 = _promote(tmp_tracking, fixture_readings)
    client = _client(tmp_tracking, gen_store, FakeTrigger())
    run_id = client.post("/demo/generate", json={"n_units": 4, "days": 1}).json()["run_id"]
    monkeypatch.setattr(generate, "run_generation", lambda spec: _synthetic_fleet())
    worker.execute_run(run_id, generate.GenerationSpec(n_units=4, days=1), gen_store)

    first = client.get(f"/demo/generate/{run_id}/report").json()
    assert first["model_version"] == v1
    assert len(gen_store.load_report(run_id, v1)) == 4        # cached under v1

    # Promote a second version; the cache key moves with it.
    v2 = _promote(tmp_tracking, fixture_readings)
    assert v2 != v1
    client.app.state.store.clear()                            # what a rollback/promotion does

    second = client.get(f"/demo/generate/{run_id}/report").json()
    assert second["model_version"] == v2
    assert len(gen_store.load_report(run_id, v2)) == 4        # re-scored, not stale
    assert second["n_units"] == first["n_units"]
