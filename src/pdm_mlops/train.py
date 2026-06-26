"""Training layer — model-selection-as-an-MLOps-process (F2).

This is the MVP core: it doesn't just fit a model, it runs an *honest comparison* and
leaves a tracked trail of it. For each contender (:mod:`models`) it opens an MLflow
run, logs the params and the primary metric (:data:`config.PRIMARY_METRIC`, ROC-AUC),
and logs the fitted model as an artifact. It then picks the winner by that metric and
**registers** it in the MLflow Model Registry — so "which model is current, and on
what evidence" is recorded, not folklore (ADR-004).

Everything runs against a **local file MLflow backend** (no server, no paid service);
the URI is overridable so tests point it at a tmp dir. The data source is injectable
so tests feed the offline fixture without touching the full-regeneration path, while
the real ``pdm train`` regenerates the full dataset (ADR-001).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import mlflow
import pandas as pd
from sklearn.metrics import roc_auc_score

from . import config, data, features, models


@dataclass(frozen=True)
class RunResult:
    """One contender's tracked outcome — name, score, and its MLflow run id."""

    name: str
    metric: float
    run_id: str


@dataclass(frozen=True)
class TrainSummary:
    """The comparison's result: every run, the winner, and its registry version."""

    results: list[RunResult]
    winner: RunResult
    registered_version: str | None

    @property
    def metric_name(self) -> str:
        return config.PRIMARY_METRIC


def _flavor_log_model(model: models.Model, name: str):
    """Log a fitted model with the right MLflow flavor (lightgbm vs. sklearn).

    The sklearn flavor is pinned to cloudpickle: MLflow 3's newer ``skops`` default
    refuses to serialise the pipeline (it flags ``numpy.dtype`` as an untrusted type),
    and cloudpickle round-trips the full Pipeline faithfully for our own reload.
    """
    if model.name == "lightgbm":
        return mlflow.lightgbm.log_model(model.estimator, name=name)
    return mlflow.sklearn.log_model(
        model.estimator,
        name=name,
        serialization_format=mlflow.sklearn.SERIALIZATION_FORMAT_CLOUDPICKLE,
    )


class DegenerateSplit(ValueError):
    """The test split has a single class, so ROC-AUC is undefined.

    Only reachable on the tiny smoke fixture, where holding out a few units by group
    can land an all-negative test set. The full dataset's 134-unit split is class-rich
    on both sides (ADR-003); this guard fails loudly instead of logging a meaningless
    ``nan`` metric (which would also collide in the registry).
    """


def _score(model: models.Model, ds: features.Dataset) -> float:
    """ROC-AUC of the positive-class probability on the held-out, unit-disjoint test."""
    if ds.y_test.nunique() < 2:
        raise DegenerateSplit(
            "the held-out test set has a single class; ROC-AUC is undefined. "
            "Pick a seed whose unit-grouped split is class-rich on both sides "
            "(only an issue on the reduced smoke fixture; the full dataset is fine)."
        )
    proba = model.predict_proba(ds.X_test)
    return float(roc_auc_score(ds.y_test, proba))


def train(
    *,
    seed: int | None = None,
    tracking_uri: str | None = None,
    register: bool = True,
    readings: pd.DataFrame | None = None,
    load: Callable[..., pd.DataFrame] | None = None,
    tuned: dict[str, dict[str, object]] | None = None,
    clean: bool | None = None,
    audit: bool = False,
    diagnose: bool = False,
) -> TrainSummary:
    """Train every contender, track each to MLflow, and register the winner.

    Args:
        seed: threads data split → models so the comparison is reproducible
            (defaults to :data:`config.DEFAULT_SEED`).
        tracking_uri: MLflow tracking/registry URI; defaults to the local
            ``mlruns/`` file backend. Tests pass a tmp ``file:`` URI.
        register: register the winner in the MLflow Model Registry (default on).
        readings: an explicit ``readings`` frame, used as-is (tests pass the
            fixture). When ``None``, the frame is obtained via ``load``.
        load: how to obtain ``readings`` when none is given — defaults to
            :func:`data.load_readings` (the full-regeneration real path, ADR-001).
        tuned: F2.6 tuned ``overrides`` per model name (from :mod:`tune`); a model
            with no entry trains at its baseline. When given, the cleaned F2.5 frame
            is used by default (the tuner searched on it) unless ``clean`` says
            otherwise.
        clean: train on the F2.5-cleaned frame (``signal_suspect`` feature on). Defaults
            to ``True`` when ``tuned`` is given (consistency with the search), else
            ``False`` (the untuned F2 baseline frame).
        audit: run the F2.6 training watchers (overfit-gap + majority-baseline) on each
            fit and **raise** on a tripped one — the opt-in ``--audit`` guard.
        diagnose: log the F2.6 diagnostic artifacts (importance, calibration, threshold
            sweep, learning curve) to each model's MLflow run.

    Returns:
        A :class:`TrainSummary` with every run's score, the winner, and the
        registered model version (``None`` when ``register=False``).
    """
    if seed is None:
        seed = config.DEFAULT_SEED
    if tracking_uri is None:
        config.MLRUNS_DIR.mkdir(parents=True, exist_ok=True)
        tracking_uri = config.sqlite_tracking_uri(config.MLFLOW_DB)
    if readings is None:
        readings = (load or data.load_readings)()
    if clean is None:
        clean = tuned is not None

    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(config.EXPERIMENT_NAME)

    ds = features.prepare(readings, seed=seed, suspect_feature=clean)

    results: list[RunResult] = []
    for model in models.build_all(seed=seed, tuned=tuned):
        with mlflow.start_run(run_name=model.name) as run:
            mlflow.log_params(model.params)
            mlflow.log_param("seed", seed)
            mlflow.log_param("cleaned_inputs", clean)
            mlflow.log_param("tuned", bool(tuned and model.name in tuned))
            mlflow.log_param("n_train_units", ds.groups_train.nunique())
            mlflow.log_param("n_test_units", ds.groups_test.nunique())
            model.fit(ds.X_train, ds.y_train)
            metric = _score(model, ds)
            mlflow.log_metric(config.PRIMARY_METRIC, metric)
            _flavor_log_model(model, name="model")
            if diagnose:
                from . import diagnostics

                diagnostics.log_diagnostics(model, ds)
            if audit:
                from . import diagnostics, tune

                cv_auc = tune._grouped_cv_auc(model, ds)
                report = diagnostics.audit_fit(
                    model, ds, cv_auc=cv_auc, test_auc=metric, strict=True
                )
                mlflow.log_metric("cv_roc_auc", report.cv_auc)
                mlflow.log_metric("train_roc_auc", report.train_auc)
            results.append(RunResult(name=model.name, metric=metric, run_id=run.info.run_id))

    # Winner = best primary metric; ties break on the fixed contender order (stable).
    winner = max(results, key=lambda r: r.metric)

    registered_version: str | None = None
    if register:
        result = mlflow.register_model(
            model_uri=f"runs:/{winner.run_id}/model",
            name=config.REGISTERED_MODEL_NAME,
        )
        registered_version = result.version

    return TrainSummary(results=results, winner=winner, registered_version=registered_version)


def format_summary(summary: TrainSummary) -> str:
    """A compact, human-readable report for the CLI."""
    lines = [f"Model comparison ({summary.metric_name}):"]
    for r in sorted(summary.results, key=lambda r: r.metric, reverse=True):
        mark = "  <- winner" if r.name == summary.winner.name else ""
        lines.append(f"  {r.name:<10} {r.metric:.4f}{mark}")
    if summary.registered_version is not None:
        lines.append(
            f"Registered '{config.REGISTERED_MODEL_NAME}' "
            f"v{summary.registered_version} ({summary.winner.name})."
        )
    else:
        lines.append("(winner not registered: register=False)")
    return "\n".join(lines)
