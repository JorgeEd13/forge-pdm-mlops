"""F5 flow tests — the drift → retrain → gated-promote loop, in-process.

The F5 DoD: the Prefect flow runs **in-process** on the fixture, the drift branch fires
and a model is promoted, and — the honesty guarantee — the promotion goes through the
*same* F3 gate, so a worse candidate is held rather than shipped. All offline: MLflow
points at a per-test tmp SQLite registry, the readings frames are injected (a synthetic
shift stands in for the generator's ``season`` stimulus), and Prefect runs on its default
local runner (no server). Both ``[ops]`` (Prefect + Evidently) libraries are optional, so
the module skips cleanly in core CI (mirrors the ``[serve]`` skip in test_serve.py).
"""

from __future__ import annotations

import pandas as pd
import pytest

pytest.importorskip("prefect", reason="needs the `[ops]` extra (F5/ADR-013)")
pytest.importorskip("evidently", reason="needs the `[ops]` extra (F5/ADR-013)")

from mlflow.tracking import MlflowClient  # noqa: E402

from pdm_mlops import config, flows, registry, train  # noqa: E402

NAME = config.REGISTERED_MODEL_NAME


@pytest.fixture
def tmp_tracking(tmp_path):
    """A throwaway SQLite MLflow backend so runs/registry never leak between tests."""
    return config.sqlite_tracking_uri(tmp_path / "mlflow.db")


@pytest.fixture
def fixture_readings() -> pd.DataFrame:
    """The committed offline smoke fixture (reduced slice — not a training set)."""
    return pd.read_parquet(config.SAMPLE_READINGS)


def _shift_thermals(readings: pd.DataFrame, delta: float = 25.0) -> pd.DataFrame:
    """A synthetic multi-signal shift standing in for ``season='heatwave'`` offline."""
    shifted = readings.copy()
    for col in ("coolant_temp_c", "egt_c", "oil_pressure_kpa", "boost_pressure_kpa"):
        shifted[col] = shifted[col] + delta
    return shifted


def _seed_production(tmp_tracking: str, readings: pd.DataFrame) -> str:
    """Train + promote an initial production model so the loop has an incumbent."""
    summary = train.train(
        seed=0, tracking_uri=tmp_tracking, readings=readings, register=True
    )
    client = MlflowClient(tracking_uri=tmp_tracking, registry_uri=tmp_tracking)
    result = registry.promote(client, NAME, summary.registered_version)
    assert result.promoted
    return str(summary.registered_version)


# --- the DoD: drift fires the loop and a model is promoted --------------------


def test_drift_triggers_retrain_and_promotes(tmp_tracking, fixture_readings) -> None:
    incumbent = _seed_production(tmp_tracking, fixture_readings)
    current = _shift_thermals(fixture_readings)

    result = flows.run_drift_retrain(
        seed=0,
        tracking_uri=tmp_tracking,
        reference=fixture_readings,
        current=current,
    )

    # Drift detected → the retrain branch fired.
    assert result.drift.drifted is True
    assert result.retrained is True
    assert result.promotion is not None

    # A new version was registered and it went through the F3 gate.
    client = MlflowClient(tracking_uri=tmp_tracking, registry_uri=tmp_tracking)
    prod = registry.production_version(client, NAME)
    if result.promoted:
        # The retrained candidate cleared the gate → production moved to the new version.
        assert prod != incumbent
        assert prod == result.promotion.candidate_version
    else:
        # A held candidate is a *valid* governed outcome — production stayed put.
        assert prod == incumbent


# --- no drift: the loop holds, nothing is retrained --------------------------


def test_no_drift_holds_the_production_model(tmp_tracking, fixture_readings) -> None:
    incumbent = _seed_production(tmp_tracking, fixture_readings)

    result = flows.run_drift_retrain(
        seed=0,
        tracking_uri=tmp_tracking,
        reference=fixture_readings,
        current=fixture_readings.copy(),  # identical → no drift
    )

    assert result.drift.drifted is False
    assert result.retrained is False
    assert result.promotion is None
    assert result.promoted is False

    # Production is untouched.
    client = MlflowClient(tracking_uri=tmp_tracking, registry_uri=tmp_tracking)
    assert registry.production_version(client, NAME) == incumbent


# --- the gate is the F3 gate: a worse candidate is HELD, not shipped ----------


def test_worse_retrained_candidate_is_held(tmp_tracking, fixture_readings) -> None:
    """With drift detected but the retrain scoring worse, the gate holds production.

    A high ``min_delta`` inverted: pass a *negative* tolerance so any candidate that is
    not strictly better than the incumbent is rejected — proving the loop routes through
    F3's gate and cannot auto-degrade. (min_delta = -1.0 ⇒ candidate must beat incumbent
    by >1.0 AUC, impossible ⇒ always held.)
    """
    incumbent = _seed_production(tmp_tracking, fixture_readings)
    current = _shift_thermals(fixture_readings)

    result = flows.run_drift_retrain(
        seed=0,
        tracking_uri=tmp_tracking,
        reference=fixture_readings,
        current=current,
        min_delta=-1.0,
    )

    assert result.drift.drifted is True
    assert result.retrained is True
    assert result.promotion.promoted is False
    assert result.promoted is False

    # The gate held: production is still the incumbent.
    client = MlflowClient(tracking_uri=tmp_tracking, registry_uri=tmp_tracking)
    assert registry.production_version(client, NAME) == incumbent


def test_summary_reflects_the_outcome(tmp_tracking, fixture_readings) -> None:
    _seed_production(tmp_tracking, fixture_readings)
    result = flows.run_drift_retrain(
        seed=0,
        tracking_uri=tmp_tracking,
        reference=fixture_readings,
        current=fixture_readings.copy(),
    )
    text = result.summary()
    assert "stable" in text
    assert "No retrain" in text
