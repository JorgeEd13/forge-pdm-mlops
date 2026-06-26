"""F2.6 diagnostics + training-watcher tests.

Offline on the committed fixture, MLflow → tmp SQLite. These assert the F2.6 DoD for the
instrumentation half: diagnostics land as MLflow artifacts, the overfit-gap and
majority-baseline watchers fire on a constructed bad fit, and the watchers pass a healthy
one. matplotlib may or may not be installed — the CSVs land either way.
"""

from __future__ import annotations

import mlflow
import pytest
from mlflow.tracking import MlflowClient

from pdm_mlops import config, data, diagnostics, features, models

FIXTURE_SEED = 0


@pytest.fixture(scope="module")
def fixture_readings():
    return data.load_fixture()


@pytest.fixture(scope="module")
def dataset(fixture_readings):
    return features.prepare(fixture_readings, seed=FIXTURE_SEED, suspect_feature=True)


@pytest.fixture(scope="module")
def fitted_lgbm(dataset):
    model = models.build_lightgbm(seed=FIXTURE_SEED)
    model.fit(dataset.X_train, dataset.y_train)
    return model


def test_feature_importance_includes_signal_suspect(fitted_lgbm, dataset) -> None:
    fi = diagnostics.feature_importance(fitted_lgbm, dataset.feature_names)
    names = {f for f, _ in fi}
    assert "signal_suspect" in names  # the cleaned frame carries the F2.5 feature
    # Importances are non-negative and the list is sorted descending.
    vals = [v for _, v in fi]
    assert all(v >= 0 for v in vals)
    assert vals == sorted(vals, reverse=True)


def test_log_diagnostics_writes_artifacts(fitted_lgbm, dataset, tmp_path) -> None:
    uri = config.sqlite_tracking_uri(tmp_path / "mlflow.db")
    mlflow.set_tracking_uri(uri)
    mlflow.set_experiment("diag-test")
    with mlflow.start_run() as run:
        diagnostics.log_diagnostics(fitted_lgbm, dataset)
        run_id = run.info.run_id

    client = MlflowClient(tracking_uri=uri)
    arts = client.list_artifacts(run_id, f"diagnostics/{fitted_lgbm.name}")
    names = {a.path.split("/")[-1] for a in arts}
    # The numeric CSVs always land (matplotlib-independent).
    assert {"feature_importance.csv", "calibration.csv", "threshold_sweep.csv",
            "learning_curve.csv"} <= names


def test_audit_passes_a_healthy_fit(dataset) -> None:
    # The watcher's *pass* path. `audit_fit` measures the train AUC from the real fitted
    # model (it is not injectable — that's the point), so to exercise "healthy" we fit a
    # deliberately **shallow, regularised** model whose train AUC is moderate, giving a
    # genuinely small train−CV gap. A deep model would (correctly) trip — see
    # test_real_fixture_fit_trips_overfit_by_design.
    shallow = models.build_lightgbm(
        seed=FIXTURE_SEED,
        overrides={"n_estimators": 100, "num_leaves": 15, "min_child_samples": 100,
                   "reg_lambda": 10.0},
    )
    shallow.fit(dataset.X_train, dataset.y_train)
    train_auc = float(
        __import__("sklearn.metrics", fromlist=["roc_auc_score"]).roc_auc_score(
            dataset.y_train, shallow.predict_proba(dataset.X_train)
        )
    )
    # Pin cv just under the train AUC so the gap is below the limit, and a clear lift.
    report = diagnostics.audit_fit(
        shallow, dataset, cv_auc=train_auc - 0.05, test_auc=0.85
    )
    assert report.beats_majority
    assert not report.overfit_tripped
    assert not report.tripped


def test_real_fixture_fit_trips_overfit_by_design(fitted_lgbm, dataset) -> None:
    # The flip side, kept honest: a deep model on the tiny fixture genuinely overfits its
    # few train units (train AUC ≈ 1.0, grouped-CV ≈ 0.6). The watcher *should* catch that
    # — that's the watcher earning its keep, not a bug. (Documented in STATE/ADR-006.)
    from pdm_mlops import tune

    cv_auc = tune._grouped_cv_auc(fitted_lgbm, dataset)
    test_auc = float(
        __import__("sklearn.metrics", fromlist=["roc_auc_score"]).roc_auc_score(
            dataset.y_test, fitted_lgbm.predict_proba(dataset.X_test)
        )
    )
    report = diagnostics.audit_fit(fitted_lgbm, dataset, cv_auc=cv_auc, test_auc=test_auc)
    assert report.overfit_tripped  # train ≈ 1.0 vs. CV ≈ 0.6 on 15 units
    assert report.beats_majority   # it does still beat trivial on held-out units


def test_overfit_watcher_fires_and_raises_in_strict(fitted_lgbm, dataset) -> None:
    # Force a large train−CV gap by claiming a very low CV score: the guard must trip.
    with pytest.raises(diagnostics.FitAudit):
        diagnostics.audit_fit(
            fitted_lgbm, dataset, cv_auc=0.50, test_auc=0.90, strict=True
        )


def test_majority_watcher_fires_on_no_lift(fitted_lgbm, dataset) -> None:
    # A test AUC at the majority baseline (0.5) means no lift over trivial → flagged.
    report = diagnostics.audit_fit(
        fitted_lgbm, dataset, cv_auc=0.50, test_auc=0.50
    )
    assert not report.beats_majority
    assert report.tripped
    with pytest.raises(diagnostics.FitAudit):
        diagnostics.audit_fit(
            fitted_lgbm, dataset, cv_auc=0.50, test_auc=0.50, strict=True
        )
