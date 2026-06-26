"""F2 model-layer tests — the one interface, NaN handling, and determinism.

Offline on the committed smoke fixture. These assert the *contract* the training
loop relies on: both contenders fit and emit positive-class probabilities, the
era-NULL feature frame is consumed without an upstream impute, and a fixed seed
yields a fixed fit.
"""

from __future__ import annotations

import numpy as np
import pytest

from pdm_mlops import data, features, models


@pytest.fixture(scope="module")
def split():
    return features.prepare(data.load_fixture(), seed=42)


@pytest.mark.parametrize("build", [models.build_logreg, models.build_lightgbm])
def test_model_fits_and_emits_positive_class_proba(build, split) -> None:
    model = build(seed=42).fit(split.X_train, split.y_train)
    proba = model.predict_proba(split.X_test)
    assert proba.shape == (len(split.X_test),)
    assert np.all((proba >= 0.0) & (proba <= 1.0))


def test_both_models_share_one_interface(split) -> None:
    # train.py loops over build_all() treating every item the same way.
    built = models.build_all(seed=42)
    assert [m.name for m in built] == ["logreg", "lightgbm"]
    for m in built:
        assert isinstance(m, models.Model)
        assert "model_type" in m.params and m.params["seed"] == 42


def test_feature_frame_still_carries_era_null(split) -> None:
    # The contract that justifies the model-side handling: the frame the models
    # receive still has NaN (ADR-003 — no upstream imputation).
    assert split.X_train.isna().to_numpy().any()


def test_lightgbm_consumes_nan_without_imputation(split) -> None:
    # LightGBM must fit on the raw NaN-bearing frame (native missing handling).
    model = models.build_lightgbm(seed=42).fit(split.X_train, split.y_train)
    assert model.predict_proba(split.X_test).shape == (len(split.X_test),)


def test_logreg_pipeline_imputes_internally(split) -> None:
    # LogReg can't eat NaN; the pipeline's imputer must absorb it so the fit works
    # on the same NaN-bearing frame without erroring.
    model = models.build_logreg(seed=42).fit(split.X_train, split.y_train)
    assert model.predict_proba(split.X_test).shape == (len(split.X_test),)
    # The imputer is the first pipeline step (where era-NULL is filled, not upstream).
    assert model.estimator.named_steps["impute"] is not None


def test_same_seed_same_fit(split) -> None:
    a = models.build_lightgbm(seed=7).fit(split.X_train, split.y_train)
    b = models.build_lightgbm(seed=7).fit(split.X_train, split.y_train)
    np.testing.assert_array_equal(a.predict_proba(split.X_test), b.predict_proba(split.X_test))
