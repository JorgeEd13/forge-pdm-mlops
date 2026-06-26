"""Outlier-detection ladder — robustness to the dirty inputs the generator injects.

The companion generator deliberately plants nine defect families (ADR-005), split
into **obvious** (a single signal spikes out of range) and **subtle** ones a naive
per-column check misses:

* ``joint_outlier`` — every signal is *individually* plausible, but the *combination*
  is not (700 rpm at 95 % load). Only a **multivariate** view catches it.
* ``sensor_stuck`` — an in-range value that stops moving (rolling variance → 0).
* ``sensor_drift`` — a slow, steady creep (a persistent nonzero rolling slope).

This module is the **clean-first** step before tuning (F2.6): each rung is an
unsupervised detector that runs on the **feature signals ONLY** and emits a per-row,
label-free *suspicion score* in ``[0, 1]``. The generator's ``is_outlier`` /
``anomaly_type`` labels are used **solely to score** these detectors (see
:mod:`pdm_mlops.detect_score`) and to *tune* the temporal thresholds — they are
**never** an input to a detector. That keeps the F1 leakage guard (ADR-003) sacred:
robustness is earned from raw signals exactly as it would be in production.

The ladder, cheap → adaptive:

1. :class:`MultivariateDetector` — ``IsolationForest`` + a robust-covariance
   **Mahalanobis** distance over the signal vector (the joint view).
2. :class:`TemporalDetector` — per-unit rolling variance (→ stuck) and rolling slope
   (→ drift); thresholds are derived against the labelled fixture and pinned as
   constants (ADR-005).
3. :class:`AutoencoderDetector` — a small CPU-only PyTorch autoencoder trained on the
   bulk (mostly-normal) signal distribution; reconstruction error = suspicion. It can
   flag shapes we never explicitly designed a rule for — but it must **earn its place**
   against the cheap rungs on *subtle* recall (scored honestly in :mod:`detect_score`).

**Era-NULL is not an anomaly.** Whole signals are NULL for older eras (ADR-003); that
missingness is structural, not a defect. Detectors impute (median, per fitted column)
*for their own internal math only* and never let "this column is era-NULL" inflate
suspicion — imputed positions cannot manufacture an outlier on their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np
import pandas as pd

from . import config, features

# The signals every detector operates on — the same fixed, ordered feature channels
# the model sees. Detectors NEVER see anything else (no keys, no labels).
SIGNAL_COLUMNS: tuple[str, ...] = features.FEATURE_COLUMNS

# Per-unit time order: detectors that look at trajectories sort by this first.
TIME_COLUMN: str = "timestamp_h"
GROUP_COLUMN: str = features.GROUP_COLUMN


def _as_signal_matrix(readings: pd.DataFrame) -> pd.DataFrame:
    """Project to the signal columns only, failing loudly on a missing channel."""
    missing = [c for c in SIGNAL_COLUMNS if c not in readings.columns]
    if missing:
        raise KeyError(
            f"detect: readings is missing signal columns {missing} "
            "(generator drift vs. the pinned version, ADR-001)."
        )
    return readings.loc[:, list(SIGNAL_COLUMNS)].copy()


def _equal_run_lengths(eq_prev: np.ndarray) -> np.ndarray:
    """Trailing run length of ``True`` in ``eq_prev`` at each position (vectorised).

    ``eq_prev[i]`` means "row ``i`` equals row ``i-1`` (same unit, non-NaN)". The run
    length resets to 0 at every ``False``. Equivalent to the scalar recurrence
    ``run[i] = run[i-1] + 1 if eq_prev[i] else 0`` but without a Python loop.
    """
    eq = eq_prev.astype("int64")
    # index of the most recent False (reset point) carried forward
    reset = np.where(eq == 0, np.arange(len(eq)), 0)
    np.maximum.accumulate(reset, out=reset)
    return np.arange(len(eq)) - reset


def _minmax01(x: np.ndarray) -> np.ndarray:
    """Scale a 1-D score to ``[0, 1]`` robustly (constant input → all zeros)."""
    x = np.asarray(x, dtype="float64")
    finite = x[np.isfinite(x)]
    if finite.size == 0:
        return np.zeros_like(x)
    lo, hi = float(finite.min()), float(finite.max())
    if hi <= lo:
        return np.zeros_like(x)
    out = (x - lo) / (hi - lo)
    return np.clip(np.nan_to_num(out, nan=0.0), 0.0, 1.0)


# --------------------------------------------------------------------------- #
# The detector interface                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DetectionResult:
    """A detector's per-row suspicion score plus the column means it imputed with.

    ``scores`` is aligned to ``readings`` row order, in ``[0, 1]`` (higher = more
    suspect). ``name`` identifies the rung in the scored comparison table.
    """

    name: str
    scores: np.ndarray


class Detector(Protocol):
    """Common surface: fit on (mostly-normal) signals, score any signal frame.

    Detectors are unsupervised — ``fit`` never receives labels. ``score`` returns a
    suspicion score per row of ``readings``, label-free.
    """

    name: str

    def fit(self, readings: pd.DataFrame) -> "Detector": ...

    def score(self, readings: pd.DataFrame) -> DetectionResult: ...


# --------------------------------------------------------------------------- #
# Rung 1 — Multivariate (joint outliers)                                       #
# --------------------------------------------------------------------------- #


@dataclass
class MultivariateDetector:
    """Joint-implausibility detector: ``IsolationForest`` + robust **Mahalanobis**.

    Per-column range checks pass a ``joint_outlier`` (each signal is in range); the
    *combination* is what is wrong. Two complementary multivariate views:

    * **IsolationForest** isolates points that sit in a sparse region of the joint
      distribution — cheap, distribution-free, no covariance assumption.
    * **Mahalanobis** distance under a **robust** covariance (``MinCovDet``) measures
      "how many correlated-sigmas off the centre", and is resistant to the very
      contamination it is trying to flag.

    The two scores are min-max'd to ``[0, 1]`` and combined by the elementwise max —
    a point is suspect if *either* view flags it. Era-NULL is median-imputed for the
    math only (``impute_means_`` are stored from ``fit``); imputed cells revert to the
    centre and so cannot themselves create suspicion.
    """

    name: str = "multivariate"
    contamination: float = 0.02
    seed: int = config.DEFAULT_SEED
    #: Cap on rows used to *fit* the robust covariance (MinCovDet is O(n) per its many
    #: subset trials and gets slow past a few thousand rows). A seeded subsample of this
    #: size estimates an 8-D covariance perfectly well; ALL rows are still scored. Keeps
    #: the offline suite fast and the production fit bounded; deterministic via ``seed``.
    cov_fit_cap: int = 4000

    def __post_init__(self) -> None:
        self.impute_means_: pd.Series | None = None
        self._iforest = None
        self._robust_cov = None

    def _prep(self, readings: pd.DataFrame, *, fitting: bool) -> np.ndarray:
        X = _as_signal_matrix(readings)
        if fitting:
            self.impute_means_ = X.median(numeric_only=True)
        assert self.impute_means_ is not None, "MultivariateDetector.score before fit"
        return X.fillna(self.impute_means_).to_numpy(dtype="float64")

    def fit(self, readings: pd.DataFrame) -> "MultivariateDetector":
        from sklearn.covariance import MinCovDet
        from sklearn.ensemble import IsolationForest

        Xm = self._prep(readings, fitting=True)
        self._iforest = IsolationForest(
            n_estimators=200,
            contamination=self.contamination,
            random_state=self.seed,
            n_jobs=1,
        ).fit(Xm)
        # Fit the robust covariance on a seeded subsample (MinCovDet is slow past a few
        # thousand rows); a higher support_fraction stabilises it on clean batches (the
        # default 0.5 can warn about an increasing determinant) while still resisting the
        # ~2% planted contamination. All rows are scored regardless of the fit sample.
        cov_fit = Xm
        if len(Xm) > self.cov_fit_cap:
            rng = np.random.default_rng(self.seed)
            idx = rng.choice(len(Xm), size=self.cov_fit_cap, replace=False)
            cov_fit = Xm[np.sort(idx)]
        self._robust_cov = MinCovDet(
            support_fraction=0.9, random_state=self.seed
        ).fit(cov_fit)
        return self

    def score(self, readings: pd.DataFrame) -> DetectionResult:
        if self._iforest is None or self._robust_cov is None:
            raise RuntimeError("MultivariateDetector.score before fit")
        Xm = self._prep(readings, fitting=False)
        # IsolationForest: lower score_samples = more anomalous → negate to rank up.
        iso = _minmax01(-self._iforest.score_samples(Xm))
        maha = _minmax01(self._robust_cov.mahalanobis(Xm))
        combined = np.maximum(iso, maha)
        return DetectionResult(name=self.name, scores=combined)


# --------------------------------------------------------------------------- #
# Rung 2 — Temporal (stuck / drift)                                            #
# --------------------------------------------------------------------------- #

#: A signal is "freeze-detectable" (continuous) iff, in normal operation, it almost
#: never repeats its previous value **exactly**. ``fit`` measures each signal's
#: baseline exact-repeat rate and keeps only those below this fraction; quantised or
#: clamped signals (oil pressure / boost, which legitimately plateau) are excluded so
#: they cannot manufacture false freezes. This selection is **unsupervised** — it reads
#: only the signals, never the labels.
CONTINUOUS_REPEAT_MAX: float = 0.01

#: A frozen signal is flagged only once it has repeated its exact value for a run of at
#: least this many consecutive in-unit samples — a single coincidental equal pair is
#: not a stuck sensor. Derived against the labelled fixture (precision/recall trade,
#: ADR-005) and pinned.
STUCK_MIN_RUN: int = 4

#: A drift is a *sustained monotonic creep*: over a window of this many samples, a
#: signal moves in one direction for at least :data:`DRIFT_MONOTONE_FRAC` of its steps.
#: A short window catches normal transients and yields false alarms; this length +
#: fraction were chosen for **high precision** (a clean ramp, few false positives — the
#: right bias for a model feature) at the cost of recall (ADR-005).
DRIFT_WINDOW: int = 12
DRIFT_MONOTONE_FRAC: float = 0.9

#: Drift detection only makes sense on signals that are **not normally monotone**: a
#: legitimately monotone channel (e.g. DEF level, which always decreases as it is
#: consumed) would otherwise read as "drifting" forever. ``fit`` measures each signal's
#: baseline monotone-window fraction and keeps it drift-eligible only if that fraction
#: is below this bound — again **unsupervised**, signals only.
DRIFT_BASELINE_MONOTONE_MAX: float = 0.15


@dataclass
class TemporalDetector:
    """Per-unit trajectory detector for ``sensor_stuck`` and ``sensor_drift``.

    The earlier rolling-variance / rolling-slope formulation could not separate these
    from normal operation (its best label-tuned F1 was ~0.02 — see ADR-005). The
    failure was diagnostic: a *stuck* sensor is not "low variance" (a running engine's
    other signals keep moving and drown it) — it is **one signal repeating its exact
    value** while the rest move; a *drift* is not "a steep window" — it is a **sustained
    monotonic creep**. Detecting those signatures directly is what works.

    Operates on each unit's time-ordered series independently (sorted by
    :data:`TIME_COLUMN`):

    * **stuck** — per signal, a run of ``≥`` :data:`STUCK_MIN_RUN` consecutive
      **exact-equal** samples. Only signals ``fit`` flagged as *continuous* (baseline
      exact-repeat rate ``<`` :data:`CONTINUOUS_REPEAT_MAX`) are eligible, so naturally
      plateauing channels can't fire.
    * **drift** — per signal, a window of :data:`DRIFT_WINDOW` samples that moves in one
      direction for ``≥`` :data:`DRIFT_MONOTONE_FRAC` of its steps (a clean ramp).

    Per-row suspicion is the max across the eligible signals. Era-NULL columns
    contribute nothing (NaN steps are not equal and not monotone). **Unsupervised**:
    ``fit`` learns only which signals are continuous, never the labels — the labels are
    used solely to *tune* the constants above (ADR-005), never as an input.
    """

    name: str = "temporal"
    continuous_repeat_max: float = CONTINUOUS_REPEAT_MAX
    stuck_min_run: int = STUCK_MIN_RUN
    drift_window: int = DRIFT_WINDOW
    drift_monotone_frac: float = DRIFT_MONOTONE_FRAC
    drift_baseline_monotone_max: float = DRIFT_BASELINE_MONOTONE_MAX

    def __post_init__(self) -> None:
        self.continuous_signals_: list[str] | None = None
        self.drift_signals_: list[str] | None = None

    def _monotone_flags(self, v: np.ndarray, same_unit: np.ndarray) -> np.ndarray:
        """Per-row: is this the end of a window that moves one way for ``≥`` the frac?"""
        diff = np.concatenate([[np.nan], np.diff(v)])
        diff[~same_unit] = np.nan
        sign = np.sign(diff)
        mono = (
            pd.Series(sign)
            .rolling(self.drift_window, min_periods=self.drift_window)
            .mean()
            .to_numpy()
        )
        return np.abs(np.nan_to_num(mono)) >= self.drift_monotone_frac

    def fit(self, readings: pd.DataFrame) -> "TemporalDetector":
        """Learn (unsupervised) which signals support freeze- and drift-detection.

        * *continuous* (freeze-detectable): baseline exact-repeat rate below
          :data:`CONTINUOUS_REPEAT_MAX` (quantised/plateauing signals excluded).
        * *drift-eligible*: baseline monotone-window fraction below
          :data:`DRIFT_BASELINE_MONOTONE_MAX` (always-monotone signals like DEF level
          excluded, or they read as drifting forever).

        Both selections read **only the signals** — never the labels.
        """
        X = _as_signal_matrix(readings)
        units = readings[GROUP_COLUMN].to_numpy()
        same_unit = np.concatenate([[False], units[1:] == units[:-1]])
        continuous: list[str] = []
        drift_eligible: list[str] = []
        for col in SIGNAL_COLUMNS:
            v = X[col].to_numpy(dtype="float64")
            valid = ~np.isnan(v)
            if not valid.any():
                continue  # an era-NULL column for this whole slice — nothing to detect
            eq_prev = np.concatenate([[False], v[1:] == v[:-1]]) & same_unit
            if float(eq_prev[valid].mean()) < self.continuous_repeat_max:
                continuous.append(col)
            mono = self._monotone_flags(v, same_unit)
            base = float(mono.mean()) if mono.size else 1.0
            if base < self.drift_baseline_monotone_max:
                drift_eligible.append(col)
        self.continuous_signals_ = continuous
        self.drift_signals_ = drift_eligible
        return self

    def score(self, readings: pd.DataFrame) -> DetectionResult:
        if self.continuous_signals_ is None:
            raise RuntimeError("TemporalDetector.score before fit")
        if GROUP_COLUMN not in readings.columns or TIME_COLUMN not in readings.columns:
            raise KeyError(
                f"temporal detector needs {GROUP_COLUMN!r} and {TIME_COLUMN!r} to order "
                "each unit's series."
            )

        z = _as_signal_matrix(readings)
        z[GROUP_COLUMN] = readings[GROUP_COLUMN].to_numpy()
        z[TIME_COLUMN] = readings[TIME_COLUMN].to_numpy()
        z["_row"] = np.arange(len(z))
        z = z.sort_values([GROUP_COLUMN, TIME_COLUMN], kind="stable")

        units = z[GROUP_COLUMN].to_numpy()
        same_unit = np.concatenate([[False], units[1:] == units[:-1]])
        n = len(z)
        stuck = np.zeros(n)
        drift = np.zeros(n)

        # --- stuck: per continuous signal, runs of ≥ stuck_min_run exact-equal values ---
        for col in self.continuous_signals_:
            v = z[col].to_numpy(dtype="float64")
            eq_prev = np.concatenate([[False], v[1:] == v[:-1]]) & same_unit & ~np.isnan(v)
            run = _equal_run_lengths(eq_prev)
            # A row is part of a stuck block if its trailing run ever reaches the
            # threshold — mark the row itself and the preceding stuck_min_run rows.
            ends = np.nonzero(run >= self.stuck_min_run)[0]
            for i in ends:
                stuck[i - self.stuck_min_run : i + 1] = 1.0

        # --- drift: per drift-eligible signal, a sustained monotone window ---
        for col in self.drift_signals_ or []:
            v = z[col].to_numpy(dtype="float64")
            mono = self._monotone_flags(v, same_unit)
            drift = np.maximum(drift, np.where(mono, 1.0, 0.0))

        per_row = np.maximum(stuck, drift)
        order = z["_row"].to_numpy()
        out = np.zeros(n)
        out[order] = per_row
        return DetectionResult(name=self.name, scores=out)


# --------------------------------------------------------------------------- #
# Rung 3 — Autoencoder (the adaptive headline)                                 #
# --------------------------------------------------------------------------- #


@dataclass
class AutoencoderDetector:
    """A small CPU-only PyTorch autoencoder; reconstruction error = suspicion.

    Trained on the bulk signal distribution (mostly normal — the planted outliers are a
    couple of percent), it learns the joint manifold of healthy readings. A point it
    *cannot* reconstruct well is, by definition, unlike what it saw — so it can flag
    shapes we never wrote an explicit rule for. That is the appeal; the honesty rule is
    that it must **earn its place**, scored against ground truth like the cheap rungs
    (:mod:`detect_score`), and reported plainly if it does not beat them on *subtle*
    recall.

    Deterministic: a fixed torch seed and ``n_jobs``-free CPU training. Signals are
    median-imputed (era-NULL → centre, math only) and standard-scaled from stats learnt
    in ``fit``. The ``[deep]`` extra (CPU torch) is intentionally **out of core CI**.
    """

    name: str = "autoencoder"
    hidden: int = 4
    epochs: int = 60
    lr: float = 1e-2
    seed: int = config.DEFAULT_SEED

    def __post_init__(self) -> None:
        self.mean_: pd.Series | None = None
        self.std_: pd.Series | None = None
        self._model = None

    def _standardize(self, readings: pd.DataFrame, *, fitting: bool) -> np.ndarray:
        X = _as_signal_matrix(readings)
        if fitting:
            self.mean_ = X.mean(numeric_only=True)
            std = X.std(numeric_only=True)
            self.std_ = std.replace(0.0, np.nan).fillna(1.0)
        assert self.mean_ is not None and self.std_ is not None
        Z = (X.fillna(self.mean_) - self.mean_) / self.std_
        return Z.to_numpy(dtype="float32")

    def fit(self, readings: pd.DataFrame) -> "AutoencoderDetector":
        import torch
        from torch import nn

        torch.manual_seed(self.seed)
        Z = self._standardize(readings, fitting=True)
        n_features = Z.shape[1]

        model = nn.Sequential(
            nn.Linear(n_features, self.hidden),
            nn.ReLU(),
            nn.Linear(self.hidden, n_features),
        )
        opt = torch.optim.Adam(model.parameters(), lr=self.lr)
        loss_fn = nn.MSELoss()
        data = torch.from_numpy(Z)
        model.train()
        for _ in range(self.epochs):
            opt.zero_grad()
            recon = model(data)
            loss = loss_fn(recon, data)
            loss.backward()
            opt.step()
        model.eval()
        self._model = model
        return self

    def score(self, readings: pd.DataFrame) -> DetectionResult:
        import torch

        if self._model is None:
            raise RuntimeError("AutoencoderDetector.score before fit")
        Z = self._standardize(readings, fitting=False)
        with torch.no_grad():
            recon = self._model(torch.from_numpy(Z)).numpy()
        err = ((Z - recon) ** 2).mean(axis=1)
        return DetectionResult(name=self.name, scores=_minmax01(err))


# --------------------------------------------------------------------------- #
# Ladder assembly                                                              #
# --------------------------------------------------------------------------- #


def build_ladder(*, seed: int | None = None, include_autoencoder: bool = True) -> list[Detector]:
    """The detection ladder, cheap → adaptive, in fixed order (determinism).

    ``include_autoencoder=False`` drops the ``[deep]`` torch rung so the cheap rungs run
    with no extra dependency (and CI never needs torch).
    """
    if seed is None:
        seed = config.DEFAULT_SEED
    ladder: list[Detector] = [
        MultivariateDetector(seed=seed),
        TemporalDetector(),
    ]
    if include_autoencoder:
        ladder.append(AutoencoderDetector(seed=seed))
    return ladder


def fit_score_all(
    readings: pd.DataFrame,
    *,
    seed: int | None = None,
    include_autoencoder: bool = True,
) -> dict[str, np.ndarray]:
    """Fit every rung on ``readings`` and return ``{detector_name: scores}``.

    Unsupervised end to end — no labels touched. Each rung fits and scores on the same
    frame (the F2.5 fixture run); the F5 loop will fit on a baseline and score a shifted
    batch, but the surface is the same.
    """
    out: dict[str, np.ndarray] = {}
    for det in build_ladder(seed=seed, include_autoencoder=include_autoencoder):
        det.fit(readings)
        out[det.name] = det.score(readings).scores
    return out
