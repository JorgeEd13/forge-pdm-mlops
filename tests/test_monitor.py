"""F5 drift-monitoring tests — the Evidently report + our decision policy.

All offline: Evidently runs on the committed smoke fixture with a **synthetic** shift
(the generator's real ``season`` stimulus isn't installed in core CI), so these assert
the *decision plumbing* — that a genuine multi-signal shift is called drift and an
identical frame is called stable, under our share threshold — not the generator's
physics. Evidently is the optional ``[ops]`` extra, so the whole module skips cleanly
when it is absent (mirrors the ``[serve]`` skip in test_serve.py).
"""

from __future__ import annotations

import pandas as pd
import pytest

pytest.importorskip("evidently", reason="needs the `[ops]` extra (F5/ADR-013)")

from pdm_mlops import config, features, monitor  # noqa: E402


@pytest.fixture
def fixture_readings() -> pd.DataFrame:
    """The committed offline smoke fixture (reduced slice — not a training set)."""
    return pd.read_parquet(config.SAMPLE_READINGS)


def _shift_thermals(readings: pd.DataFrame, delta: float = 25.0) -> pd.DataFrame:
    """A synthetic heatwave-like shift: push the thermal signals up by ``delta``.

    Stands in for the generator's ``season='heatwave'`` stimulus offline — a real,
    multi-column distribution shift across the correlated thermal signals, which is
    exactly what the share-threshold policy should fire on.
    """
    shifted = readings.copy()
    for col in ("coolant_temp_c", "egt_c", "oil_pressure_kpa", "boost_pressure_kpa"):
        shifted[col] = shifted[col] + delta
    return shifted


# --- the decision: a real shift is drift, an identical frame is not -----------


def test_shifted_data_is_flagged_as_drift(fixture_readings) -> None:
    current = _shift_thermals(fixture_readings)
    report = monitor.detect_drift(reference=fixture_readings, current=current)

    assert report.drifted is True
    assert report.share_drifted >= report.threshold
    # The shift moved several signals, so more than one feature must have drifted.
    assert report.n_drifted >= 2
    assert set(report.by_feature) <= set(features.FEATURE_COLUMNS)


def test_identical_data_is_not_drift(fixture_readings) -> None:
    # Reference vs. an identical copy: no feature should drift, so no retrain trigger.
    report = monitor.detect_drift(
        reference=fixture_readings, current=fixture_readings.copy()
    )
    assert report.drifted is False
    assert report.n_drifted == 0
    assert report.share_drifted == 0.0


# --- the report structure + our policy ---------------------------------------


def test_report_covers_exactly_the_feature_columns(fixture_readings) -> None:
    # Drift is monitored on the model's inputs — no more, no less.
    _, ev = monitor.drift_report(fixture_readings, _shift_thermals(fixture_readings))
    distilled, _ = monitor.drift_report(
        fixture_readings, _shift_thermals(fixture_readings)
    )
    assert distilled.n_features == len(features.FEATURE_COLUMNS)
    assert set(distilled.by_feature) == set(features.FEATURE_COLUMNS)


def test_decision_uses_the_configured_threshold(fixture_readings) -> None:
    # The DriftReport records the policy it decided under (auditable), and it is the
    # single configured threshold — not Evidently's own default dataset-drift flag.
    report = monitor.detect_drift(
        reference=fixture_readings, current=_shift_thermals(fixture_readings)
    )
    assert report.threshold == config.DRIFT_SHARE_THRESHOLD
    assert report.drifted == (report.share_drifted >= config.DRIFT_SHARE_THRESHOLD)


def test_summary_is_human_readable(fixture_readings) -> None:
    report = monitor.detect_drift(
        reference=fixture_readings, current=_shift_thermals(fixture_readings)
    )
    text = report.summary()
    assert "DRIFT" in text
    assert "threshold" in text
