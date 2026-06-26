"""Feature layer — turn ``readings`` into a leakage-safe modelling frame.

Three responsibilities (the F1 contract):

1. **Select features vs. target honestly.** Features are the J1939 sensor signals
   only. The target is :data:`config.TARGET` (``failure_within_h``). Everything that
   *describes the failure itself* — ``failure_mode``, ``anomaly_type``,
   ``is_outlier`` — is **label-side bookkeeping** and is excluded from features: it
   is only known because the failure already happened, so using it would leak the
   answer. :func:`assert_no_leakage` makes that a hard, tested guarantee.

2. **Preserve era-NULL missingness.** Some signals are entirely NULL for whole units
   (older eras that lack that sensor — e.g. no EGT/DEF on a pre-emissions machine).
   That missingness is a *real, informative pattern*, so we do **not** impute it
   here. LightGBM consumes NaN natively; the LogReg pipeline imputes at model time
   (F2), keeping the feature frame faithful to the source.

3. **Split BY UNIT.** Train and test never share a ``unit_id`` — a unit's rows are an
   autocorrelated time series, so a random row split would leak a unit's behaviour
   across the boundary and inflate the score. A seeded grouped split keeps the
   evaluation honest and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from sklearn.model_selection import GroupShuffleSplit

from . import config

#: The model's input signals — the J1939 sensor channels only. Order is fixed so the
#: feature matrix is stable across runs (determinism). Era-NULL values are preserved.
FEATURE_COLUMNS: tuple[str, ...] = (
    "engine_speed_rpm",
    "coolant_temp_c",
    "oil_pressure_kpa",
    "engine_load_pct",
    "fuel_rate_lph",
    "boost_pressure_kpa",
    "egt_c",
    "def_level_pct",
)

#: Columns that must NEVER become features: the target itself plus label-side
#: bookkeeping that is only knowable *because* the failure occurred. Used both to
#: drop them and to assert (loudly) that none leaked into ``X``.
LEAKY_COLUMNS: tuple[str, ...] = (
    config.TARGET,        # the answer
    "failure_mode",       # which failure — known only once it has happened
    "anomaly_type",       # the injected-anomaly label
    "is_outlier",         # outlier bookkeeping tied to the same labelling pass
)

#: The grouping key for the split — one unit's rows stay together (train XOR test).
GROUP_COLUMN: str = "unit_id"

#: Fraction of *units* (not rows) held out for the test set.
TEST_SIZE: float = 0.25


@dataclass(frozen=True)
class Dataset:
    """A leakage-safe, unit-disjoint modelling split.

    ``X_*`` carry only :data:`FEATURE_COLUMNS` (era-NULL preserved); ``y_*`` are the
    binary target; ``groups_*`` are the ``unit_id`` of each row (handy for grouped
    cross-validation and for asserting the split is disjoint).
    """

    X_train: pd.DataFrame
    X_test: pd.DataFrame
    y_train: pd.Series
    y_test: pd.Series
    groups_train: pd.Series
    groups_test: pd.Series

    @property
    def feature_names(self) -> list[str]:
        return list(self.X_train.columns)


def assert_no_leakage(X: pd.DataFrame) -> None:
    """Raise if any target/label-side column slipped into the feature matrix.

    A cheap, explicit guard that runs on every prepared frame so a future column
    rename or a careless edit cannot silently leak the answer into training.
    """
    leaked = [c for c in LEAKY_COLUMNS if c in X.columns]
    if leaked:
        raise ValueError(
            f"leakage guard: target/label-side columns present in features: {leaked}. "
            "Features must be sensor signals only (see features.LEAKY_COLUMNS)."
        )


def select_features(readings: pd.DataFrame) -> pd.DataFrame:
    """Project ``readings`` onto the fixed feature signals, era-NULLs preserved.

    Missing feature columns (a generator-version drift) fail loudly rather than
    silently producing a narrower matrix.
    """
    missing = [c for c in FEATURE_COLUMNS if c not in readings.columns]
    if missing:
        raise KeyError(
            f"readings is missing expected feature columns {missing}; "
            "the generator version may have drifted from the pinned one (ADR-001)."
        )
    X = readings.loc[:, list(FEATURE_COLUMNS)].copy()
    assert_no_leakage(X)  # belt-and-braces: the projection itself stays clean
    return X


def prepare(
    readings: pd.DataFrame,
    *,
    seed: int | None = None,
    suspect_feature: bool = False,
    suspect_use_autoencoder: bool = False,
) -> Dataset:
    """Build the leakage-safe, unit-disjoint train/test split.

    Deterministic: the same ``readings`` + ``seed`` always yield the same split
    (the grouped splitter is seeded). ``seed`` defaults to
    :data:`config.DEFAULT_SEED`.

    ``suspect_feature`` appends the F2.5 ``signal_suspect`` column (the detection
    ladder's combined, **signal-derived** suspicion — :mod:`pdm_mlops.suspect`) to the
    feature matrix so the classifier can learn to distrust suspect rows. It is computed
    from signals only and re-passes :func:`assert_no_leakage`, so the leakage guard
    stays intact. ``suspect_use_autoencoder`` folds the ``[deep]`` torch rung into that
    score (off by default → no extra dependency).
    """
    if seed is None:
        seed = config.DEFAULT_SEED

    for required in (config.TARGET, GROUP_COLUMN):
        if required not in readings.columns:
            raise KeyError(f"readings is missing the required column {required!r}.")

    X = select_features(readings)
    if suspect_feature:
        # Local import: suspect → detect pull in sklearn/optional torch; keep the base
        # feature path import-light and only wire the ladder when explicitly asked.
        from . import suspect as _suspect

        X = X.copy()
        X[_suspect.SUSPECT_COLUMN] = _suspect.compute_suspect(
            readings, seed=seed, use_autoencoder=suspect_use_autoencoder
        )
        assert_no_leakage(X)  # the augmented frame must still be label-free
    y = readings[config.TARGET].astype("int8")
    groups = readings[GROUP_COLUMN]

    splitter = GroupShuffleSplit(n_splits=1, test_size=TEST_SIZE, random_state=seed)
    train_idx, test_idx = next(splitter.split(X, y, groups))

    ds = Dataset(
        X_train=X.iloc[train_idx].reset_index(drop=True),
        X_test=X.iloc[test_idx].reset_index(drop=True),
        y_train=y.iloc[train_idx].reset_index(drop=True),
        y_test=y.iloc[test_idx].reset_index(drop=True),
        groups_train=groups.iloc[train_idx].reset_index(drop=True),
        groups_test=groups.iloc[test_idx].reset_index(drop=True),
    )

    # Disjointness is the whole point of a grouped split — assert it, don't assume it.
    overlap = set(ds.groups_train.unique()) & set(ds.groups_test.unique())
    if overlap:
        raise AssertionError(f"unit(s) in both train and test: {sorted(overlap)}")
    return ds
