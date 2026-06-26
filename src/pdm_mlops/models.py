"""Model layer — the two contenders behind one ``fit``/``predict_proba`` interface.

The model is deliberately *not* the point of this project (ADR-002): the plumbing
around it is. So this stays small — two cheap estimators that train in seconds on a
GPU-less desktop, compared *through MLflow* as an honest model-selection process
(ADR-004):

* **``logreg``** — a scikit-learn :class:`~sklearn.pipeline.Pipeline`:
  median-impute → standard-scale → :class:`~sklearn.linear_model.LogisticRegression`.
  LogReg cannot consume NaN, so the era-NULL missingness the feature layer preserved
  (ADR-003) is imputed **here, at model time** — the feature frame stays faithful to
  the source and only this model fills the gaps.
* **``lightgbm``** — a :class:`~lightgbm.LGBMClassifier`. It consumes NaN natively, so
  it sees the era-NULL pattern as a real signal: **no imputation, no scaling**.

Both expose the same surface (:class:`Model`), so :mod:`train` can fit, score and log
them in a single loop without caring which is which. Every estimator is seeded from
one place so the same seed → the same fitted model → the same metric.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from . import config


@runtime_checkable
class Estimator(Protocol):
    """The minimal scikit-learn-style surface both contenders satisfy."""

    def fit(self, X: pd.DataFrame, y: pd.Series) -> object: ...

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray: ...


@dataclass(frozen=True)
class Model:
    """A named contender plus the params worth logging to MLflow.

    ``estimator`` is an unfitted scikit-learn-style object (a Pipeline or an
    LGBMClassifier); ``params`` is the flat, JSON-friendly description that gets
    logged alongside the run so a tracked run is self-explaining.
    """

    name: str
    estimator: Estimator
    params: dict[str, object]

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "Model":
        self.estimator.fit(X, y)
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """Probability of the positive class (``failure_within_h == 1``)."""
        proba = self.estimator.predict_proba(X)
        return np.asarray(proba)[:, 1]


#: LogReg hyper-parameters F2.6's HPO may override (everything else is fixed structure).
#: Defaults match the untuned baseline so ``build_logreg()`` with no overrides is the F2
#: model exactly. ``tune.py`` proposes values inside these names; unknown keys are rejected.
LOGREG_TUNABLE: tuple[str, ...] = ("C",)

#: LightGBM hyper-parameters F2.6's HPO may override (the tree budget + regularisation).
LIGHTGBM_TUNABLE: tuple[str, ...] = (
    "n_estimators",
    "learning_rate",
    "num_leaves",
    "min_child_samples",
    "reg_lambda",
)


def _check_overrides(overrides: dict[str, object], allowed: tuple[str, ...], who: str) -> None:
    """Reject a tuned-param key the model doesn't expose, instead of silently dropping it."""
    unknown = [k for k in overrides if k not in allowed]
    if unknown:
        raise ValueError(
            f"{who}: unknown hyper-parameter(s) {unknown}; tunable keys are {list(allowed)}."
        )


def build_logreg(*, seed: int | None = None, overrides: dict[str, object] | None = None) -> Model:
    """LogReg baseline: median-impute → standard-scale → logistic regression.

    Imputation lives **in the pipeline** (not the feature layer) so the era-NULL
    missingness stays intact upstream and only this model fills it (ADR-003). Scaling
    matters for LogReg's regularised coefficients; ``class_weight="balanced"`` offsets
    the low failure base rate.

    ``overrides`` carries tuned hyper-parameters from F2.6's HPO (keys restricted to
    :data:`LOGREG_TUNABLE`); with none, this is the untuned F2 baseline exactly.
    """
    if seed is None:
        seed = config.DEFAULT_SEED
    overrides = dict(overrides or {})
    _check_overrides(overrides, LOGREG_TUNABLE, "build_logreg")
    C = float(overrides.get("C", 1.0))
    estimator = Pipeline(
        steps=[
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
            (
                "clf",
                LogisticRegression(
                    C=C,
                    max_iter=1000,
                    class_weight="balanced",
                    random_state=seed,
                ),
            ),
        ]
    )
    params = {
        "model_type": "logistic_regression",
        "impute": "median",
        "scale": "standard",
        "class_weight": "balanced",
        "max_iter": 1000,
        "C": C,
        "seed": seed,
    }
    return Model(name="logreg", estimator=estimator, params=params)


def build_lightgbm(*, seed: int | None = None, overrides: dict[str, object] | None = None) -> Model:
    """LightGBM contender: native NaN handling, no imputation, no scaling.

    The era-NULL pattern is fed in as-is — LightGBM routes NaN down its own branch, so
    "which sensor era is this unit" stays an available signal. ``class_weight``
    balances the rare positive class; the tree budget is small (cheap on the i3).

    ``overrides`` carries tuned hyper-parameters from F2.6's HPO (keys restricted to
    :data:`LIGHTGBM_TUNABLE`); with none, this is the untuned F2 baseline exactly.
    """
    if seed is None:
        seed = config.DEFAULT_SEED
    overrides = dict(overrides or {})
    _check_overrides(overrides, LIGHTGBM_TUNABLE, "build_lightgbm")
    hp: dict[str, object] = {
        "n_estimators": 200,
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_child_samples": 20,
        "reg_lambda": 0.0,
    }
    hp.update(overrides)
    estimator = LGBMClassifier(
        n_estimators=int(hp["n_estimators"]),
        learning_rate=float(hp["learning_rate"]),
        num_leaves=int(hp["num_leaves"]),
        min_child_samples=int(hp["min_child_samples"]),
        reg_lambda=float(hp["reg_lambda"]),
        class_weight="balanced",
        random_state=seed,
        n_jobs=1,  # deterministic; the dataset is small enough not to need threads
        verbose=-1,
    )
    params = {
        "model_type": "lightgbm",
        "class_weight": "balanced",
        "seed": seed,
        **hp,
    }
    return Model(name="lightgbm", estimator=estimator, params=params)


#: Map each contender name to its builder — so ``tune`` and ``train`` can address a
#: model by name and feed it the tuned ``overrides`` without a branchy if/else.
BUILDERS = {"logreg": build_logreg, "lightgbm": build_lightgbm}


def build_all(
    *, seed: int | None = None, tuned: dict[str, dict[str, object]] | None = None
) -> list[Model]:
    """The full contender field, in a fixed order (determinism of the comparison).

    ``tuned`` maps a model name to its tuned ``overrides`` (from F2.6's HPO); a model
    with no entry is built at its baseline defaults. With ``tuned=None`` this is the
    untuned F2 field exactly.
    """
    tuned = tuned or {}
    return [
        build(seed=seed, overrides=tuned.get(name))
        for name, build in BUILDERS.items()
    ]
