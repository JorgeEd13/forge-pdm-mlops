"""F2.6 HPO tests — a tracked, unit-grouped Optuna study on the cleaned inputs.

All offline: the data source is the committed fixture (passed in, so the full-
regeneration path is never touched), MLflow points at a per-test tmp SQLite backend, and
the trial budget is tiny (the search behaviour is what's under test, not convergence).
These assert the F2.6 DoD for tuning: grouped CV never leaks a unit across folds, the
search is deterministic (same seed → same best params), and the study is tracked.
"""

from __future__ import annotations

import importlib.util

import mlflow
import pytest
from mlflow.tracking import MlflowClient

from pdm_mlops import config, data, features, models, tune

# Optuna lives in the optional `[tune]` extra (ADR-006) — the package, CLI, and CI stay
# light without it (tune.py imports it lazily). The tests that actually run a study are
# marked with this and skipped when the extra isn't installed, rather than forcing optuna
# into core CI. The grouped-CV geometry test below needs no optuna and always runs.
needs_optuna = pytest.mark.skipif(
    importlib.util.find_spec("optuna") is None,
    reason="optuna not installed (optional `[tune]` extra — ADR-006)",
)

# Seed whose unit-grouped split is class-rich on both sides of every fold (the default 42
# lands a single-class fixture slice — see test_train.py). The fixture is small, so a
# class-rich seed keeps the grouped-CV folds well-formed.
FIXTURE_SEED = 0


@pytest.fixture(scope="module")
def fixture_readings():
    return data.load_fixture()


@pytest.fixture(scope="module")
def cleaned_dataset(fixture_readings):
    # The tuner searches on the F2.5-cleaned frame; build it once for the module.
    return features.prepare(fixture_readings, seed=FIXTURE_SEED, suspect_feature=True)


@pytest.fixture
def tmp_tracking(tmp_path):
    return config.sqlite_tracking_uri(tmp_path / "mlflow.db")


def test_grouped_cv_never_shares_a_unit_across_folds(cleaned_dataset) -> None:
    # The whole point of grouped CV: a unit is never in both train and val of a fold.
    ds = cleaned_dataset
    from sklearn.model_selection import GroupKFold

    n_splits = min(tune.N_SPLITS, ds.groups_train.nunique())
    cv = GroupKFold(n_splits=n_splits)
    for tr, va in cv.split(ds.X_train, ds.y_train, ds.groups_train):
        train_units = set(ds.groups_train.iloc[tr])
        val_units = set(ds.groups_train.iloc[va])
        assert train_units.isdisjoint(val_units)


@needs_optuna
def test_tune_model_returns_a_score_and_only_tunable_params(cleaned_dataset) -> None:
    result = tune.tune_model("lightgbm", cleaned_dataset, seed=FIXTURE_SEED, n_trials=5)
    assert result.name == "lightgbm"
    assert 0.0 <= result.best_value <= 1.0
    # Every returned key is a declared tunable for that model — no stray params.
    assert set(result.best_params) <= set(models.LIGHTGBM_TUNABLE)
    assert result.n_trials == 5


@needs_optuna
def test_tuned_params_actually_build_a_model(cleaned_dataset) -> None:
    # The contract that feeds train(): best_params must be valid overrides.
    result = tune.tune_model("logreg", cleaned_dataset, seed=FIXTURE_SEED, n_trials=5)
    model = models.build_logreg(seed=FIXTURE_SEED, overrides=result.best_params)
    model.fit(cleaned_dataset.X_train, cleaned_dataset.y_train)
    proba = model.predict_proba(cleaned_dataset.X_test)
    assert proba.shape[0] == len(cleaned_dataset.X_test)


@needs_optuna
def test_tune_is_deterministic(cleaned_dataset) -> None:
    a = tune.tune_model("lightgbm", cleaned_dataset, seed=FIXTURE_SEED, n_trials=8)
    b = tune.tune_model("lightgbm", cleaned_dataset, seed=FIXTURE_SEED, n_trials=8)
    assert a.best_params == b.best_params
    assert a.best_value == b.best_value


@needs_optuna
def test_tune_tracks_one_run_per_model(fixture_readings, tmp_tracking) -> None:
    results = tune.tune(
        seed=FIXTURE_SEED, n_trials=4, tracking_uri=tmp_tracking, readings=fixture_readings
    )
    assert set(results) == {"logreg", "lightgbm"}

    client = MlflowClient(tracking_uri=tmp_tracking)
    exp = client.get_experiment_by_name(f"{config.EXPERIMENT_NAME}-tune")
    runs = client.search_runs([exp.experiment_id])
    assert len(runs) == 2
    for run in runs:
        assert f"cv_{config.PRIMARY_METRIC}" in run.data.metrics


@needs_optuna
def test_tune_searches_the_cleaned_frame(monkeypatch, fixture_readings, tmp_tracking) -> None:
    # tune() must prepare with suspect_feature=True (the cleaned F2.5 frame), so the
    # search optimises over the signal_suspect feature. Spy on prepare's kwarg.
    seen = {}
    real_prepare = features.prepare

    def spy(readings, **kw):
        seen.update(kw)
        return real_prepare(readings, **kw)

    monkeypatch.setattr(tune.features, "prepare", spy)
    tune.tune(seed=FIXTURE_SEED, n_trials=2, tracking_uri=tmp_tracking, readings=fixture_readings)
    assert seen.get("suspect_feature") is True
