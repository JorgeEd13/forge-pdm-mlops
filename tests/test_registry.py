"""F3 registry-layer tests — governed promotion + rollback.

All offline: MLflow (tracking **and** registry) points at a per-test tmp SQLite
backend, and versions are registered from tiny hand-logged runs whose ROC-AUC we set
directly — so the metric gate can be exercised with exact better/worse candidates
without training anything. These assert the F3 DoD: **a worse candidate does not
promote**, and **rollback restores the prior production version** — plus the alias
mechanics, the force escape hatch, and the loud errors on malformed input.
"""

from __future__ import annotations

import mlflow
import pytest
from mlflow.tracking import MlflowClient
from sklearn.dummy import DummyClassifier

from pdm_mlops import config, registry

NAME = config.REGISTERED_MODEL_NAME


@pytest.fixture
def tmp_tracking(tmp_path):
    """A throwaway SQLite MLflow backend so runs/registry never leak between tests."""
    return config.sqlite_tracking_uri(tmp_path / "mlflow.db")


@pytest.fixture
def client(tmp_tracking):
    return MlflowClient(tracking_uri=tmp_tracking, registry_uri=tmp_tracking)


def _register_version(tmp_tracking: str, metric: float, *, log_metric: bool = True) -> str:
    """Log a trivial run at a chosen ROC-AUC and register it, returning the version.

    Mirrors what ``train`` does (log the primary metric on a run, then
    ``register_model`` the run's model) but with a dialled-in metric so the gate can be
    tested against exact numbers. ``log_metric=False`` registers a version whose run
    never logged the metric — to test the loud-failure path.
    """
    mlflow.set_tracking_uri(tmp_tracking)
    mlflow.set_experiment(config.EXPERIMENT_NAME)
    with mlflow.start_run() as run:
        if log_metric:
            mlflow.log_metric(config.PRIMARY_METRIC, metric)
        # A minimal real sklearn model so there is an artifact URI to register. Logged
        # with cloudpickle — the same flavor pin ADR-004 uses, so re-loading at
        # register time doesn't trip MLflow 3's skops untrusted-types guard.
        mlflow.sklearn.log_model(
            DummyClassifier(strategy="constant", constant=0).fit([[0]], [0]),
            name="model",
            serialization_format=mlflow.sklearn.SERIALIZATION_FORMAT_CLOUDPICKLE,
        )
    result = mlflow.register_model(
        model_uri=f"runs:/{run.info.run_id}/model", name=NAME
    )
    # MLflow can hand back the version as an int here; the registry surface speaks
    # strings (``ModelVersion.version`` is a str), so normalise to match.
    return str(result.version)


# --- the gate: the F3 DoD -----------------------------------------------------


def test_worse_candidate_does_not_promote(client, tmp_tracking) -> None:
    good = _register_version(tmp_tracking, 0.82)
    bad = _register_version(tmp_tracking, 0.70)

    # Establish production with the good version.
    first = registry.promote(client, NAME, good)
    assert first.promoted
    assert registry.production_version(client, NAME) == good

    # The worse candidate is rejected and the alias does NOT move.
    result = registry.promote(client, NAME, bad)
    assert result.promoted is False
    assert result.incumbent_version == good
    assert result.candidate_metric == pytest.approx(0.70)
    assert result.incumbent_metric == pytest.approx(0.82)
    assert registry.production_version(client, NAME) == good


def test_better_candidate_promotes(client, tmp_tracking) -> None:
    old = _register_version(tmp_tracking, 0.80)
    new = _register_version(tmp_tracking, 0.85)

    registry.promote(client, NAME, old)
    result = registry.promote(client, NAME, new)

    assert result.promoted
    assert registry.production_version(client, NAME) == new


def test_first_promotion_has_no_incumbent(client, tmp_tracking) -> None:
    # With nothing in production yet, any version promotes (there is nothing to beat).
    only = _register_version(tmp_tracking, 0.51)
    result = registry.promote(client, NAME, only)
    assert result.promoted
    assert result.incumbent_version is None
    assert result.incumbent_metric is None
    assert registry.production_version(client, NAME) == only


def test_tie_promotes_by_default(client, tmp_tracking) -> None:
    # min_delta default 0.0 → an equal-scoring newer version wins (documented choice).
    old = _register_version(tmp_tracking, 0.80)
    new = _register_version(tmp_tracking, 0.80)
    registry.promote(client, NAME, old)
    result = registry.promote(client, NAME, new)
    assert result.promoted
    assert registry.production_version(client, NAME) == new


def test_min_delta_tolerates_a_small_regression(client, tmp_tracking) -> None:
    old = _register_version(tmp_tracking, 0.82)
    new = _register_version(tmp_tracking, 0.815)  # 0.005 worse
    registry.promote(client, NAME, old)

    # Default gate rejects the regression...
    assert registry.promote(client, NAME, new).promoted is False
    # ...but a 0.01 tolerance accepts it.
    result = registry.promote(client, NAME, new, min_delta=0.01)
    assert result.promoted
    assert registry.production_version(client, NAME) == new


def test_force_bypasses_the_gate(client, tmp_tracking) -> None:
    good = _register_version(tmp_tracking, 0.82)
    bad = _register_version(tmp_tracking, 0.60)
    registry.promote(client, NAME, good)

    result = registry.promote(client, NAME, bad, gate=False)
    assert result.promoted
    assert "forced" in result.reason
    assert registry.production_version(client, NAME) == bad


# --- rollback -----------------------------------------------------------------


def test_rollback_restores_prior_production(client, tmp_tracking) -> None:
    v1 = _register_version(tmp_tracking, 0.80)
    v2 = _register_version(tmp_tracking, 0.85)

    registry.promote(client, NAME, v1)
    registry.promote(client, NAME, v2)
    assert registry.production_version(client, NAME) == v2

    restored = registry.rollback(client, NAME)
    assert restored == v1
    assert registry.production_version(client, NAME) == v1


def test_rollback_without_predecessor_raises(client, tmp_tracking) -> None:
    only = _register_version(tmp_tracking, 0.80)
    registry.promote(client, NAME, only)  # first-ever, no predecessor tag
    with pytest.raises(registry.PromotionError):
        registry.rollback(client, NAME)


def test_rollback_with_nothing_promoted_raises(client) -> None:
    with pytest.raises(registry.PromotionError):
        registry.rollback(client, NAME)


# --- loud failures on malformed input ----------------------------------------


def test_promote_unknown_version_raises(client) -> None:
    with pytest.raises(registry.PromotionError):
        registry.promote(client, NAME, "999")


def test_version_without_metric_raises(client, tmp_tracking) -> None:
    v = _register_version(tmp_tracking, 0.0, log_metric=False)
    with pytest.raises(registry.PromotionError):
        registry.version_metric(client, NAME, v)


# --- helpers ------------------------------------------------------------------


def test_latest_version_is_the_highest(client, tmp_tracking) -> None:
    _register_version(tmp_tracking, 0.70)
    _register_version(tmp_tracking, 0.75)
    v3 = _register_version(tmp_tracking, 0.80)
    assert registry.latest_version(client, NAME) == v3


def test_latest_version_without_any_raises(client) -> None:
    with pytest.raises(registry.PromotionError):
        registry.latest_version(client, NAME)


def test_production_version_is_none_when_unset(client, tmp_tracking) -> None:
    _register_version(tmp_tracking, 0.80)  # registered but never promoted
    assert registry.production_version(client, NAME) is None
