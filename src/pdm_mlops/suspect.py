"""Turn the detection ladder into a leakage-safe model feature + a data-quality watcher.

F2.5's *output*. The ladder in :mod:`pdm_mlops.detect` scores each row's suspicion from
the signals alone; this module:

1. **`signal_suspect` feature** (:func:`add_signal_suspect`) — combines the rungs into
   one ``[0, 1]`` suspicion score and appends it as a **new feature column** so the
   downstream classifier can learn to distrust suspect rows. It is derived from signals
   ONLY — the F1 leakage guard (:func:`features.assert_no_leakage`, ADR-003) is run on
   the augmented frame and must still pass.

2. **Data-quality watcher** (:func:`data_quality_check`) — a forensic watcher
   ([[feedback_forensic_watchers]] pattern): it fails **loud** if a batch's suspect rate
   spikes past a fitted baseline (e.g. a sensor bank going bad, or — in F5 — a drifted
   season inflating outliers). Returns a structured report and, in ``strict`` mode,
   raises so a bad batch cannot slip through silently.

The combiner is the **mean** of the cheap, dependency-free rungs by default
(multivariate + temporal), so the feature is available without the ``[deep]`` torch
extra; the autoencoder can be folded in when present. Deterministic throughout.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import config, detect, features

#: The feature column the ladder contributes. It is a *signal-derived* suspicion score,
#: not a label — so it is explicitly NOT in ``features.LEAKY_COLUMNS``.
SUSPECT_COLUMN: str = "signal_suspect"

#: A batch's suspect rate may rise to this multiple of the fitted baseline before the
#: data-quality watcher trips. Generous enough to ignore ordinary noise, tight enough
#: to catch a sensor bank degrading or a drifted season (the F5 stimulus).
SUSPECT_RATE_SPIKE_FACTOR: float = 3.0

#: A row counts as "suspect" (for the rate watcher) when its combined score is at least
#: this high. The feature itself stays continuous; this only thresholds the *monitor*.
SUSPECT_FLAG_THRESHOLD: float = 0.5


def _combine(scores: dict[str, np.ndarray], *, use_autoencoder: bool) -> np.ndarray:
    """Combine per-rung scores into one ``[0, 1]`` suspicion. Mean of available rungs."""
    names = list(scores)
    if not use_autoencoder:
        names = [n for n in names if n != "autoencoder"]
    if not names:
        raise ValueError("no detector rungs available to combine into signal_suspect.")
    stacked = np.vstack([scores[n] for n in names])
    return np.clip(stacked.mean(axis=0), 0.0, 1.0)


def compute_suspect(
    readings: pd.DataFrame,
    *,
    seed: int | None = None,
    use_autoencoder: bool = False,
) -> np.ndarray:
    """Fit the ladder on ``readings`` and return the combined ``[0, 1]`` suspicion score.

    Signals only — labels are never read here. ``use_autoencoder`` is off by default so
    the feature is computable without the ``[deep]`` extra (CI-safe); turn it on once
    the ladder has been scored and the AE has earned its place (ADR-005).
    """
    scores = detect.fit_score_all(readings, seed=seed, include_autoencoder=use_autoencoder)
    return _combine(scores, use_autoencoder=use_autoencoder)


def add_signal_suspect(
    readings: pd.DataFrame,
    *,
    seed: int | None = None,
    use_autoencoder: bool = False,
) -> pd.DataFrame:
    """Return ``readings`` with a leakage-safe :data:`SUSPECT_COLUMN` appended.

    The new column is signal-derived; the augmented frame is passed through
    :func:`features.assert_no_leakage` (over the model's feature view) to *prove* the
    feature did not smuggle in a label. The classifier may then use ``signal_suspect``
    alongside the raw signals.
    """
    out = readings.copy()
    out[SUSPECT_COLUMN] = compute_suspect(readings, seed=seed, use_autoencoder=use_autoencoder)
    # Belt-and-braces: the augmented feature view must still be label-free.
    feature_view = out[[*features.FEATURE_COLUMNS, SUSPECT_COLUMN]]
    features.assert_no_leakage(feature_view)
    return out


@dataclass(frozen=True)
class DataQualityReport:
    """The data-quality watcher's verdict on a batch."""

    n_rows: int
    suspect_rate: float
    baseline_rate: float
    spike_factor: float
    tripped: bool

    def summary(self) -> str:
        verb = "TRIPPED" if self.tripped else "ok"
        return (
            f"data-quality [{verb}]: suspect_rate={self.suspect_rate:.4f} "
            f"(baseline={self.baseline_rate:.4f}, allowed up to "
            f"{self.baseline_rate * self.spike_factor:.4f} = {self.spike_factor:g}x)."
        )


def data_quality_check(
    readings: pd.DataFrame,
    *,
    baseline_rate: float,
    seed: int | None = None,
    use_autoencoder: bool = False,
    spike_factor: float = SUSPECT_RATE_SPIKE_FACTOR,
    flag_threshold: float = SUSPECT_FLAG_THRESHOLD,
    strict: bool = False,
) -> DataQualityReport:
    """Forensic watcher: fail loud if a batch's suspect rate spikes past baseline.

    ``baseline_rate`` is the suspect-flag rate measured on healthy reference data
    (:func:`fit_baseline_rate`). A batch trips when its rate exceeds
    ``baseline_rate × spike_factor`` — a sensor bank going bad, or the F5 drifted season
    inflating outliers. In ``strict`` mode a trip **raises** :class:`DataQualitySpike`
    so a bad batch cannot pass silently; otherwise the caller inspects ``.tripped``.
    """
    suspicion = compute_suspect(readings, seed=seed, use_autoencoder=use_autoencoder)
    rate = float((suspicion >= flag_threshold).mean())
    tripped = rate > baseline_rate * spike_factor
    report = DataQualityReport(
        n_rows=len(readings),
        suspect_rate=rate,
        baseline_rate=baseline_rate,
        spike_factor=spike_factor,
        tripped=tripped,
    )
    if tripped and strict:
        raise DataQualitySpike(report.summary())
    return report


def fit_baseline_rate(
    readings: pd.DataFrame,
    *,
    seed: int | None = None,
    use_autoencoder: bool = False,
    flag_threshold: float = SUSPECT_FLAG_THRESHOLD,
) -> float:
    """The suspect-flag rate on reference (healthy-baseline) data — the watcher's anchor."""
    suspicion = compute_suspect(readings, seed=seed, use_autoencoder=use_autoencoder)
    return float((suspicion >= flag_threshold).mean())


class DataQualitySpike(RuntimeError):
    """Raised by :func:`data_quality_check` in strict mode when a batch trips."""
