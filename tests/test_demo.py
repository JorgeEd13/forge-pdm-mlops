"""F7 demo-UI tests — the /demo page and /demo/predict endpoint (ADR-015).

Offline, like test_serve.py: a tiny model is trained on the committed fixture, registered,
and promoted to ``production`` in a tmp SQLite MLflow backend, and the demo endpoints are
driven through a ``TestClient``. These assert (1) the demo prediction round-trips exactly
like ``/predict``, (2) when a prediction log is injected the served rows are **persisted**
and (3) surface on the ``/demo`` page, and (4) with **no** log the demo still works and
says so — the graceful-degrade contract. Needs both the ``[serve]`` and ``[cloud]`` extras
for the persistence tests; the no-persistence tests need only ``[serve]``.
"""

from __future__ import annotations

import pandas as pd
import pytest
from mlflow.tracking import MlflowClient

pytest.importorskip("fastapi", reason="needs the `[serve]` extra (F4/ADR-009)")
pytest.importorskip("httpx", reason="needs the `[serve]` extra (F4/ADR-009)")

from fastapi.testclient import TestClient  # noqa: E402

from pdm_mlops import config, features, registry, serve, store_pg, train  # noqa: E402

NAME = config.REGISTERED_MODEL_NAME

_HAS_CLOUD = True
try:  # the [cloud] extra — the persistence half of the demo
    import sqlalchemy  # noqa: F401
except ImportError:  # pragma: no cover - exercised where [cloud] is absent
    _HAS_CLOUD = False

needs_cloud = pytest.mark.skipif(not _HAS_CLOUD, reason="needs the `[cloud]` extra (F7/ADR-015)")


@pytest.fixture
def tmp_tracking(tmp_path):
    return config.sqlite_tracking_uri(tmp_path / "mlflow.db")


@pytest.fixture
def fixture_readings() -> pd.DataFrame:
    return pd.read_parquet(config.SAMPLE_READINGS)


def _train_and_promote(tmp_tracking: str, readings: pd.DataFrame) -> str:
    """Train on the fixture (class-rich seed), register + promote — return the version."""
    summary = train.train(seed=0, tracking_uri=tmp_tracking, readings=readings, register=True)
    client = MlflowClient(tracking_uri=tmp_tracking, registry_uri=tmp_tracking)
    assert registry.promote(client, NAME, summary.registered_version).promoted
    return str(summary.registered_version)


def _rows(readings: pd.DataFrame, n: int = 2) -> list[dict]:
    frame = readings.loc[:, list(features.FEATURE_COLUMNS)].head(n)
    return [
        {k: (None if pd.isna(v) else float(v)) for k, v in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _app(tmp_tracking: str, log: store_pg.PredictionLog | None):
    store = serve.ModelStore(tracking_uri=tmp_tracking)
    return TestClient(serve.create_app(store=store, prediction_log=log))


# --- the demo prediction round-trips (no persistence) -------------------------


def test_demo_predict_round_trips_without_a_log(tmp_tracking, fixture_readings) -> None:
    _train_and_promote(tmp_tracking, fixture_readings)
    client = _app(tmp_tracking, log=None)

    resp = client.post("/demo/predict", json={"readings": _rows(fixture_readings, 2)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["n_rows"] == 2
    assert all(0.0 <= p <= 1.0 for p in body["failure_probability"])
    # No DATABASE_URL → not persisted, and the response says so honestly.
    assert body["persisted"] is False


def test_demo_predict_503_without_a_promoted_model(tmp_tracking, fixture_readings) -> None:
    # Same 503 contract as /predict when nothing is promoted (not a 500).
    client = _app(tmp_tracking, log=None)
    resp = client.post("/demo/predict", json={"readings": _rows(fixture_readings, 1)})
    assert resp.status_code == 503


def test_demo_page_renders_with_the_honesty_banner(tmp_tracking, fixture_readings) -> None:
    _train_and_promote(tmp_tracking, fixture_readings)
    client = _app(tmp_tracking, log=None)

    resp = client.get("/demo")
    assert resp.status_code == 200
    page = resp.text
    # The demo=fixture honesty boundary is on the page (matches /model-info + README).
    assert "DEMO model" in page
    assert "not" in page.lower() and "reported result" in page.lower()
    # Every feature signal has a form field.
    assert all(sig in page for sig in features.FEATURE_COLUMNS)
    # With no DB the page says logging is off (no managed-DB panel).
    assert "off" in page.lower()


# --- persistence to the managed DB (the F7 gate-relevant half) ----------------


@needs_cloud
def test_demo_predict_persists_to_the_log(tmp_tracking, fixture_readings, tmp_path) -> None:
    version = _train_and_promote(tmp_tracking, fixture_readings)
    log = store_pg.open_log(f"sqlite:///{(tmp_path / 'demo.db').as_posix()}")
    client = _app(tmp_tracking, log=log)

    resp = client.post("/demo/predict", json={"readings": _rows(fixture_readings, 3)})
    assert resp.status_code == 200
    assert resp.json()["persisted"] is True

    # The three served rows were logged (newest-first), tagged with the served version.
    recent = log.recent(limit=10)
    assert len(recent) == 3
    assert all(r.model_version == version for r in recent)
    log.dispose()


@needs_cloud
def test_demo_page_shows_recent_predictions(tmp_tracking, fixture_readings, tmp_path) -> None:
    _train_and_promote(tmp_tracking, fixture_readings)
    log = store_pg.open_log(f"sqlite:///{(tmp_path / 'demo.db').as_posix()}")
    client = _app(tmp_tracking, log=log)

    client.post("/demo/predict", json={"readings": _rows(fixture_readings, 1)})
    page = client.get("/demo").text
    # With a DB configured the page advertises the managed Postgres panel and a row table.
    assert "Postgres" in page
    assert "<table" in page
    log.dispose()
