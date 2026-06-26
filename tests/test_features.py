"""F1 feature-layer tests — leakage guard, unit-disjoint split, determinism, era-NULL.

All offline on the committed smoke fixture; these are correctness guarantees the
modelling phases (F2+) build on, so they assert the *contract*, not just shapes.
"""

from __future__ import annotations

import numpy as np
import pytest

from pdm_mlops import config, data, features


@pytest.fixture(scope="module")
def readings():
    return data.load_fixture()


def test_features_are_signals_only_no_leakage(readings) -> None:
    X = features.select_features(readings)
    assert list(X.columns) == list(features.FEATURE_COLUMNS)
    for leaky in features.LEAKY_COLUMNS:
        assert leaky not in X.columns
    # The guard must actually fire if a leaky column sneaks in.
    bad = X.copy()
    bad[config.TARGET] = 0
    with pytest.raises(ValueError, match="leakage guard"):
        features.assert_no_leakage(bad)


def test_split_is_unit_disjoint(readings) -> None:
    ds = features.prepare(readings)
    train_units = set(ds.groups_train.unique())
    test_units = set(ds.groups_test.unique())
    assert train_units and test_units
    assert train_units.isdisjoint(test_units)
    # Every row is accounted for exactly once.
    assert len(ds.X_train) + len(ds.X_test) == len(readings)


def test_split_is_deterministic(readings) -> None:
    a = features.prepare(readings, seed=42)
    b = features.prepare(readings, seed=42)
    assert set(a.groups_test.unique()) == set(b.groups_test.unique())
    # Same seed → identical feature matrices, row for row.
    assert a.X_train.equals(b.X_train)
    assert a.y_test.equals(b.y_test)


def test_different_seed_can_change_the_partition(readings) -> None:
    a = features.prepare(readings, seed=1)
    b = features.prepare(readings, seed=2)
    # Not a hard guarantee for every pair, but with this fixture the held-out unit
    # sets differ — documents that the seed actually drives the partition.
    assert set(a.groups_test.unique()) != set(b.groups_test.unique())


def test_era_null_missingness_is_preserved(readings) -> None:
    # The fixture has whole units that are NULL for era-gated signals (e.g. egt_c);
    # the feature layer must NOT impute that away — it is a real, informative signal.
    assert readings["egt_c"].isna().any()
    X = features.select_features(readings)
    assert X.isna().to_numpy().any()
    # And the NaN count per signal is carried through untouched.
    for col in features.FEATURE_COLUMNS:
        assert int(X[col].isna().sum()) == int(readings[col].isna().sum())


def test_target_is_binary_int(readings) -> None:
    ds = features.prepare(readings)
    assert set(np.unique(ds.y_train)).issubset({0, 1})
    assert ds.y_train.dtype == np.dtype("int8")
