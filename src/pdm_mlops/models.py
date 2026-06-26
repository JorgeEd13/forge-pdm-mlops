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


def build_logreg(*, seed: int | None = None) -> Model:
    """LogReg baseline: median-impute → standard-scale → logistic regression.

    Imputation lives **in the pipeline** (not the feature layer) so the era-NULL
    missingness stays intact upstream and only this model fills it (ADR-003). Scaling
    matters for LogReg's regularised coefficients; ``class_weight="balanced"`` offsets
    the low failure base rate.
    """
    if seed is None:
        seed = config.DEFAULT_SEED
    estimator = Pipeline(
        steps=[
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
            (
                "clf",
                LogisticRegression(
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
        "seed": seed,
    }
    return Model(name="logreg", estimator=estimator, params=params)


def build_lightgbm(*, seed: int | None = None) -> Model:
    """LightGBM contender: native NaN handling, no imputation, no scaling.

    The era-NULL pattern is fed in as-is — LightGBM routes NaN down its own branch, so
    "which sensor era is this unit" stays an available signal. ``class_weight``
    balances the rare positive class; the tree budget is small (cheap on the i3).
    """
    if seed is None:
        seed = config.DEFAULT_SEED
    estimator = LGBMClassifier(
        n_estimators=200,
        learning_rate=0.05,
        num_leaves=31,
        class_weight="balanced",
        random_state=seed,
        n_jobs=1,  # deterministic; the dataset is small enough not to need threads
        verbose=-1,
    )
    params = {
        "model_type": "lightgbm",
        "n_estimators": 200,
        "learning_rate": 0.05,
        "num_leaves": 31,
        "class_weight": "balanced",
        "seed": seed,
    }
    return Model(name="lightgbm", estimator=estimator, params=params)


def build_all(*, seed: int | None = None) -> list[Model]:
    """The full contender field, in a fixed order (determinism of the comparison)."""
    return [build_logreg(seed=seed), build_lightgbm(seed=seed)]
