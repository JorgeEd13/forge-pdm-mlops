"""F2.5 detection-ladder tests — the rungs run, are deterministic, and stay label-free.

Offline on the committed smoke fixture. The autoencoder rung needs the ``[deep]`` torch
extra; its tests skip cleanly when torch is absent (CI never installs it).
"""

from __future__ import annotations

import numpy as np
import pytest

from pdm_mlops import data, detect, features


@pytest.fixture(scope="module")
def readings():
    return data.load_fixture()


def _has_torch() -> bool:
    try:
        import torch  # noqa: F401
    except Exception:
        return False
    return True


needs_torch = pytest.mark.skipif(not _has_torch(), reason="needs the [deep] torch extra")


def test_cheap_rungs_score_in_unit_range(readings) -> None:
    for det in (detect.MultivariateDetector(), detect.TemporalDetector()):
        det.fit(readings)
        scores = det.score(readings).scores
        assert scores.shape == (len(readings),)
        assert np.isfinite(scores).all()
        assert scores.min() >= 0.0 and scores.max() <= 1.0


def test_detectors_are_label_free(readings) -> None:
    # Dropping the label columns must not change any detector's output — proof the
    # rungs read signals only (the F1 leakage guard's spirit, at detection time).
    signal_only = readings.drop(columns=list(features.LEAKY_COLUMNS), errors="ignore")
    for det_a, det_b in (
        (detect.MultivariateDetector(), detect.MultivariateDetector()),
        (detect.TemporalDetector(), detect.TemporalDetector()),
    ):
        a = det_a.fit(readings).score(readings).scores
        b = det_b.fit(signal_only).score(signal_only).scores
        np.testing.assert_allclose(a, b)


def test_multivariate_is_deterministic(readings) -> None:
    a = detect.MultivariateDetector(seed=42).fit(readings).score(readings).scores
    b = detect.MultivariateDetector(seed=42).fit(readings).score(readings).scores
    np.testing.assert_allclose(a, b)


def test_temporal_selects_continuous_signals_unsupervised(readings) -> None:
    det = detect.TemporalDetector().fit(readings)
    # fit learns which signals it can freeze-/drift-detect; both lists are subsets of
    # the signal channels and exclude nothing label-side.
    assert det.continuous_signals_ is not None and det.drift_signals_ is not None
    for col in (*det.continuous_signals_, *det.drift_signals_):
        assert col in detect.SIGNAL_COLUMNS


def test_temporal_does_not_flag_everything(readings) -> None:
    # The whole point of the rewrite (ADR-005): the temporal rung must be discriminative,
    # not a flag-everything detector. On the strided fixture it fires on a small minority.
    scores = detect.TemporalDetector().fit(readings).score(readings).scores
    assert (scores > 0).mean() < 0.20


def test_temporal_needs_time_and_group_columns(readings) -> None:
    det = detect.TemporalDetector().fit(readings)
    with pytest.raises(KeyError):
        det.score(readings.drop(columns=[detect.TIME_COLUMN]))


def test_missing_signal_column_fails_loud(readings) -> None:
    det = detect.MultivariateDetector().fit(readings)
    with pytest.raises(KeyError, match="missing signal columns"):
        det.score(readings.drop(columns=["engine_speed_rpm"]))


def test_equal_run_lengths_matches_scalar_recurrence() -> None:
    eq = np.array([False, True, True, True, False, True, False], dtype=bool)
    got = detect._equal_run_lengths(eq)
    # scalar reference
    ref = np.zeros(len(eq), dtype="int64")
    for i in range(1, len(eq)):
        ref[i] = ref[i - 1] + 1 if eq[i] else 0
    np.testing.assert_array_equal(got, ref)


def test_build_ladder_respects_autoencoder_flag(readings) -> None:
    names_cheap = [d.name for d in detect.build_ladder(include_autoencoder=False)]
    assert "autoencoder" not in names_cheap
    names_full = [d.name for d in detect.build_ladder(include_autoencoder=True)]
    assert names_full[-1] == "autoencoder"


@needs_torch
def test_autoencoder_runs_and_is_deterministic(readings) -> None:
    a = detect.AutoencoderDetector(seed=42, epochs=10).fit(readings).score(readings).scores
    b = detect.AutoencoderDetector(seed=42, epochs=10).fit(readings).score(readings).scores
    assert a.shape == (len(readings),)
    assert a.min() >= 0.0 and a.max() <= 1.0
    np.testing.assert_allclose(a, b)
