"""F4 serving-layer tests — the FastAPI app over the promoted model.

All offline: MLflow (tracking **and** registry) points at a per-test tmp SQLite backend,
a real (tiny) model is trained on the committed fixture and registered, then promoted to
the ``production`` alias — so the app resolves ``models:/<name>@production`` exactly as it
would in production, against a throwaway registry. These assert the F4 DoD: a
``TestClient`` **round-trips a prediction**, plus the health/model-info surface, the
era-NULL passthrough, and the "nothing promoted yet" 503 path.
"""

from __future__ import annotations

import mlflow
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from mlflow.tracking import MlflowClient

from pdm_mlops import config, features, registry, serve, train

NAME = config.REGISTERED_MODEL_NAME


@pytest.fixture
def tmp_tracking(tmp_path):
    """A throwaway SQLite MLflow backend so runs/registry never leak between tests."""
    return config.sqlite_tracking_uri(tmp_path / "mlflow.db")


@pytest.fixture
def fixture_readings() -> pd.DataFrame:
    """The committed offline smoke fixture (never a training set for reported metrics)."""
    return pd.read_parquet(config.SAMPLE_READINGS)


def _train_and_promote(tmp_tracking: str, readings: pd.DataFrame) -> str:
    """Train on the fixture, register the winner, promote it — return the version.

    Uses a class-rich seed so the fixture's unit-grouped split has both classes on the
    held-out side (else ``train`` raises ``DegenerateSplit``, an ADR-004 fixture
    artifact). Returns the promoted production version.
    """
    summary = train.train(
        seed=0, tracking_uri=tmp_tracking, readings=readings, register=True
    )
    client = MlflowClient(tracking_uri=tmp_tracking, registry_uri=tmp_tracking)
    result = registry.promote(client, NAME, summary.registered_version)
    assert result.promoted
    # The registry surface (and the serving response) speak str versions; MLflow can
    # hand `registered_version` back as an int, so normalise for stable comparisons.
    return str(summary.registered_version)


def _client(tmp_tracking: str) -> TestClient:
    """A TestClient over an app whose store is bound to the tmp registry."""
    app = serve.create_app(store=serve.ModelStore(tracking_uri=tmp_tracking))
    return TestClient(app)


def _sample_rows(readings: pd.DataFrame, n: int = 4) -> list[dict]:
    """A few request rows keyed by the model's feature signals (JSON-safe, era-NULL ok)."""
    frame = readings.loc[:, list(features.FEATURE_COLUMNS)].head(n)
    # NaN → None so it serialises as JSON null (the era-NULL passthrough contract).
    return [
        {k: (None if pd.isna(v) else float(v)) for k, v in row.items()}
        for row in frame.to_dict(orient="records")
    ]


# --- the DoD: a prediction round-trips ---------------------------------------


def test_predict_round_trips(tmp_tracking, fixture_readings) -> None:
    version = _train_and_promote(tmp_tracking, fixture_readings)
    client = _client(tmp_tracking)

    rows = _sample_rows(fixture_readings, n=5)
    resp = client.post("/predict", json={"readings": rows})

    assert resp.status_code == 200
    body = resp.json()
    assert body["model_version"] == version
    assert body["n_rows"] == 5
    probs = body["failure_probability"]
    assert len(probs) == 5
    assert all(0.0 <= p <= 1.0 for p in probs)


def test_predict_passes_through_era_null(tmp_tracking, fixture_readings) -> None:
    # A row with a missing signal (era-NULL as JSON null) is a valid input, not a 422.
    _train_and_promote(tmp_tracking, fixture_readings)
    client = _client(tmp_tracking)

    rows = _sample_rows(fixture_readings, n=2)
    rows[0]["egt_c"] = None  # drop a signal → era-NULL
    resp = client.post("/predict", json={"readings": rows})

    assert resp.status_code == 200
    assert len(resp.json()["failure_probability"]) == 2


def test_predict_reorders_columns(tmp_tracking, fixture_readings) -> None:
    # The frame is forced to FEATURE_COLUMNS order regardless of JSON key order.
    _train_and_promote(tmp_tracking, fixture_readings)
    client = _client(tmp_tracking)

    row = _sample_rows(fixture_readings, n=1)[0]
    shuffled = dict(reversed(list(row.items())))
    resp = client.post("/predict", json={"readings": [shuffled]})
    assert resp.status_code == 200


# --- health + model-info ------------------------------------------------------


def test_health_reports_loaded_model(tmp_tracking, fixture_readings) -> None:
    version = _train_and_promote(tmp_tracking, fixture_readings)
    client = _client(tmp_tracking)

    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["model_loaded"] is True
    assert body["model_version"] == version


def test_model_info_exposes_the_production_version(tmp_tracking, fixture_readings) -> None:
    version = _train_and_promote(tmp_tracking, fixture_readings)
    client = _client(tmp_tracking)

    resp = client.get("/model-info")
    assert resp.status_code == 200
    body = resp.json()
    assert body["registered_model"] == NAME
    assert body["production_version"] == version
    assert body["primary_metric"] == config.PRIMARY_METRIC
    assert 0.0 <= body["metric_value"] <= 1.0


# --- nothing promoted yet: healthy but not ready -----------------------------


def test_health_ok_without_a_promoted_model(tmp_tracking) -> None:
    # Empty registry: the process is up (200) but no model is loaded.
    client = _client(tmp_tracking)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["model_loaded"] is False
    assert body["model_version"] is None


def test_predict_503_without_a_promoted_model(tmp_tracking, fixture_readings) -> None:
    # No promotion → /predict is unavailable (503), not a 500.
    client = _client(tmp_tracking)
    rows = _sample_rows(fixture_readings, n=1)
    resp = client.post("/predict", json={"readings": rows})
    assert resp.status_code == 503


def test_model_info_503_without_a_promoted_model(tmp_tracking) -> None:
    client = _client(tmp_tracking)
    resp = client.get("/model-info")
    assert resp.status_code == 503


# --- rollback is picked up ----------------------------------------------------


def test_store_clear_repoints_after_rollback(tmp_tracking, fixture_readings) -> None:
    # Promote v1, then a second version, then roll back — a fresh store load sees the
    # restored version (serving follows the alias F3 moves).
    v1 = _train_and_promote(tmp_tracking, fixture_readings)
    summary2 = train.train(
        seed=11, tracking_uri=tmp_tracking, readings=fixture_readings, register=True
    )
    client = MlflowClient(tracking_uri=tmp_tracking, registry_uri=tmp_tracking)
    v2 = str(summary2.registered_version)
    registry.promote(client, NAME, v2, gate=False)
    assert registry.production_version(client, NAME) == v2

    restored = registry.rollback(client, NAME)
    assert restored == v1

    store = serve.ModelStore(tracking_uri=tmp_tracking)
    assert store.load().version == v1


# --- validation ---------------------------------------------------------------


def test_empty_readings_is_rejected(tmp_tracking, fixture_readings) -> None:
    _train_and_promote(tmp_tracking, fixture_readings)
    client = _client(tmp_tracking)
    resp = client.post("/predict", json={"readings": []})
    assert resp.status_code == 422  # min_length=1
