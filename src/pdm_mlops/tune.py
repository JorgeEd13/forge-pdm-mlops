"""Hyper-parameter search — a tracked, unit-grouped Optuna study (F2.6).

This is **instrumentation, not an accuracy play** (ADR-006): on this synthetic data the
score is faint by design, so the value is a *visible, honest search* — every trial
scored by **unit-grouped cross-validation** so the tuner can't leak a unit across folds
and undo ADR-003, the whole study tracked to MLflow, and tuned params fed forward to
:func:`train.train`.

Two studies, one per contender (:data:`models.BUILDERS`), each over the model's tunable
space (:data:`models.LOGREG_TUNABLE` / :data:`models.LIGHTGBM_TUNABLE`). The objective is
the **mean ROC-AUC across ``GroupKFold`` folds** on the *training* split only — the test
split is held back, untouched, so F2.6 never tunes against the number F2 reports. The
search runs on the **cleaned** F2.5 inputs (``features.prepare(suspect_feature=True)``),
so ``signal_suspect`` is in the matrix the tuner optimises over.

Determinism: Optuna's sampler is seeded, ``GroupKFold`` is order-deterministic, and the
models seed from one place — same seed + same data → same best params.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import mlflow
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import GroupKFold

from . import config, data, features, models

#: Folds for the grouped CV objective. Small: the search is over *units*, and the full
#: dataset's 100 train units divide cleanly; the fixture's ~15 train units still give
#: disjoint folds. Each fold holds out whole units (never a row), mirroring ADR-003.
N_SPLITS: int = 5

#: Default trial budget per model — small on purpose (cheap on the GPU-less i3, and the
#: synthetic score plateaus fast). Overridable from the CLI.
DEFAULT_TRIALS: int = 40


@dataclass(frozen=True)
class TuneResult:
    """One model's tuned outcome — the best grouped-CV score and the winning params."""

    name: str
    best_value: float
    best_params: dict[str, object]
    n_trials: int


def _grouped_cv_auc(model: models.Model, ds: features.Dataset) -> float:
    """Mean ROC-AUC over :data:`N_SPLITS` unit-grouped folds of the *training* split.

    Each fold refits a fresh estimator (built from the trial's params) on the in-fold
    units and scores the held-out units — so no unit is ever in both sides of a fold.
    A fold whose validation slice is single-class is skipped (ROC-AUC undefined there);
    if *every* fold degenerates the objective is ``nan`` and Optuna prunes the trial.
    """
    X, y, groups = ds.X_train, ds.y_train, ds.groups_train
    n_splits = min(N_SPLITS, groups.nunique())
    cv = GroupKFold(n_splits=n_splits)
    scores: list[float] = []
    for train_idx, val_idx in cv.split(X, y, groups):
        y_val = y.iloc[val_idx]
        if y_val.nunique() < 2:
            continue  # undefined ROC-AUC on a single-class fold (fixture-only artifact)
        fold = model.__class__(name=model.name, estimator=_clone(model), params=model.params)
        fold.estimator.fit(X.iloc[train_idx], y.iloc[train_idx])
        proba = np.asarray(fold.estimator.predict_proba(X.iloc[val_idx]))[:, 1]
        scores.append(float(roc_auc_score(y_val, proba)))
    return float(np.mean(scores)) if scores else float("nan")


def _clone(model: models.Model):
    """A fresh, unfitted copy of a contender's estimator (sklearn-clone for both flavors)."""
    from sklearn.base import clone

    return clone(model.estimator)


def _suggest_logreg(trial) -> dict[str, object]:
    return {"C": trial.suggest_float("C", 1e-3, 1e2, log=True)}


def _suggest_lightgbm(trial) -> dict[str, object]:
    return {
        "n_estimators": trial.suggest_int("n_estimators", 100, 600, step=50),
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
        "num_leaves": trial.suggest_int("num_leaves", 15, 127),
        "min_child_samples": trial.suggest_int("min_child_samples", 10, 100),
        "reg_lambda": trial.suggest_float("reg_lambda", 1e-3, 10.0, log=True),
    }


#: Each contender's param-suggestion function (the search space), addressed by name.
SUGGEST = {"logreg": _suggest_logreg, "lightgbm": _suggest_lightgbm}


def tune_model(
    name: str,
    ds: features.Dataset,
    *,
    seed: int | None = None,
    n_trials: int = DEFAULT_TRIALS,
) -> TuneResult:
    """Run a seeded Optuna study for one contender over its tunable space.

    The objective is :func:`_grouped_cv_auc` (mean ROC-AUC over unit-grouped folds of the
    training split). Returns the best score and params; does **not** touch MLflow (the
    orchestrating :func:`tune` logs), so this stays unit-testable in isolation.
    """
    import optuna

    if seed is None:
        seed = config.DEFAULT_SEED
    build = models.BUILDERS[name]
    suggest = SUGGEST[name]

    def objective(trial) -> float:
        overrides = suggest(trial)
        model = build(seed=seed, overrides=overrides)
        return _grouped_cv_auc(model, ds)

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=seed),
    )
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
    return TuneResult(
        name=name,
        best_value=float(study.best_value),
        best_params=dict(study.best_params),
        n_trials=n_trials,
    )


def tune(
    *,
    seed: int | None = None,
    n_trials: int = DEFAULT_TRIALS,
    tracking_uri: str | None = None,
    readings: pd.DataFrame | None = None,
    load: Callable[..., pd.DataFrame] | None = None,
) -> dict[str, TuneResult]:
    """Tune every contender on the **cleaned** inputs and track each study to MLflow.

    Builds the F2.5-cleaned modelling frame (``features.prepare(suspect_feature=True)``)
    so ``signal_suspect`` is in the matrix the tuner optimises, runs one grouped-CV
    Optuna study per model (:func:`tune_model`), and logs each study's best params +
    score as its own MLflow run under a dedicated ``-tune`` experiment. Returns
    ``{model_name: TuneResult}`` — feed ``{n: r.best_params}`` to ``train.train(tuned=…)``.

    The data source is injectable (``readings`` / ``load``) so tests stay offline; the
    real ``pdm tune`` regenerates the full dataset (ADR-001).
    """
    if seed is None:
        seed = config.DEFAULT_SEED
    if tracking_uri is None:
        config.MLRUNS_DIR.mkdir(parents=True, exist_ok=True)
        tracking_uri = config.sqlite_tracking_uri(config.MLFLOW_DB)
    if readings is None:
        readings = (load or data.load_readings)()

    ds = features.prepare(readings, seed=seed, suspect_feature=True)

    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(f"{config.EXPERIMENT_NAME}-tune")

    out: dict[str, TuneResult] = {}
    for name in models.BUILDERS:
        result = tune_model(name, ds, seed=seed, n_trials=n_trials)
        with mlflow.start_run(run_name=f"{name}-tune"):
            mlflow.log_param("model", name)
            mlflow.log_param("seed", seed)
            mlflow.log_param("n_trials", n_trials)
            mlflow.log_param("cv", f"GroupKFold(n_splits={N_SPLITS})")
            mlflow.log_params({f"best_{k}": v for k, v in result.best_params.items()})
            mlflow.log_metric(f"cv_{config.PRIMARY_METRIC}", result.best_value)
        out[name] = result
    return out


def format_tune(results: dict[str, TuneResult]) -> str:
    """A compact, human-readable report of the tuned params for the CLI."""
    lines = [f"Hyper-parameter search (grouped-CV {config.PRIMARY_METRIC}):"]
    for name, r in results.items():
        params = ", ".join(f"{k}={_fmt(v)}" for k, v in r.best_params.items())
        lines.append(f"  {name:<10} cv={r.best_value:.4f}  ({r.n_trials} trials)  [{params}]")
    return "\n".join(lines)


def _fmt(v: object) -> str:
    return f"{v:.4g}" if isinstance(v, float) else str(v)
