"""F2.5 output tests — the signal_suspect feature stays leakage-safe; the watcher fires.

Offline on the committed smoke fixture, cheap rungs only (no torch). These assert the
two F2.5 deliverables behave: a label-free model feature and a data-quality watcher.
"""

from __future__ import annotations

import numpy as np
import pytest

from pdm_mlops import data, features, suspect


@pytest.fixture(scope="module")
def readings():
    return data.load_fixture()


def test_signal_suspect_is_added_and_leakage_safe(readings) -> None:
    aug = suspect.add_signal_suspect(readings, use_autoencoder=False)
    assert suspect.SUSPECT_COLUMN in aug.columns
    s = aug[suspect.SUSPECT_COLUMN].to_numpy()
    assert np.isfinite(s).all() and s.min() >= 0.0 and s.max() <= 1.0
    # The augmented feature view must still pass the leakage guard.
    view = aug[[*features.FEATURE_COLUMNS, suspect.SUSPECT_COLUMN]]
    features.assert_no_leakage(view)  # must not raise


def test_suspect_column_is_not_a_label(readings) -> None:
    # signal_suspect is signal-derived, so it must NOT be in the leaky set, even though
    # it is informative about outliers.
    assert suspect.SUSPECT_COLUMN not in features.LEAKY_COLUMNS


def test_prepare_can_include_suspect_feature(readings) -> None:
    base = features.prepare(readings)
    aug = features.prepare(readings, suspect_feature=True)
    assert suspect.SUSPECT_COLUMN not in base.feature_names
    assert suspect.SUSPECT_COLUMN in aug.feature_names
    assert aug.X_train.shape[1] == base.X_train.shape[1] + 1


def test_signal_suspect_is_deterministic(readings) -> None:
    a = suspect.compute_suspect(readings, seed=42)
    b = suspect.compute_suspect(readings, seed=42)
    np.testing.assert_allclose(a, b)


def test_data_quality_watcher_fires_on_a_poisoned_batch(readings) -> None:
    normal = readings[~readings["is_outlier"]].reset_index(drop=True)
    baseline = max(suspect.fit_baseline_rate(normal), 1e-4)

    ok = suspect.data_quality_check(normal, baseline_rate=baseline)
    assert not ok.tripped

    # A batch heavy with planted outliers must trip the watcher.
    poisoned = readings[readings["is_outlier"]]
    bad = suspect.data_quality_check(poisoned, baseline_rate=baseline)
    assert bad.tripped
    assert bad.suspect_rate > baseline


def test_strict_mode_raises_on_spike(readings) -> None:
    normal = readings[~readings["is_outlier"]].reset_index(drop=True)
    baseline = max(suspect.fit_baseline_rate(normal), 1e-4)
    poisoned = readings[readings["is_outlier"]]
    with pytest.raises(suspect.DataQualitySpike):
        suspect.data_quality_check(poisoned, baseline_rate=baseline, strict=True)
