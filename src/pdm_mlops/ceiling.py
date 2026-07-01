"""Characterize the ceiling — is the limit the **data** or the **model**? (F2.8)

The F2.5→F2.7 arc turned one question into measured answers: tuning is exhausted
(F2.6, +0.003), temporal structure helps a little (F2.7, +0.007), and the deep TCN
does **not** earn its place. Each pointed at the same conclusion — the ceiling is a
property of the *data*, not the modelling — but so far that has been *asserted*. F2.8
is the capstone that **measures** it, so the string of nulls becomes a proven thesis
and the modelling investigation stops here by design (ADR-010).

Three instruments, all on the **exact F1 unit split / seed / test rows** as F2/F2.7
(apples-to-apples), and all **label-honest** — labels are read in this module *only to
grade or to bound*, never as a model input on the reported path (the ADR-003 guard,
exactly the ``detect_score`` discipline):

* **Decomposition** (:func:`decompose`) — the honest held-out AUC, sliced by
  **time-to-failure horizon** and by **failure mode**. Answers "is 0.82 flat, or ~0.95
  near failure and ~0.5 far out?" — most of the 168 h label window is genuinely
  healthy-and-unpredictable by construction, so a modest *aggregate* is expected even
  when the near-failure signal is strong. Time-to-failure is derived **label-side**
  (:func:`time_to_failure`) and used only to bucket, never fed to a model.

* **Label-leaking upper-bound** (:func:`upper_bound`) — a model that *sees*
  ``failure_mode`` (+ the derived time-to-failure), bounding the **irreducible** error.
  It is a **diagnostic, clearly labelled, and fenced off from any reported metric**:
  it is returned in its own field, the honest features never see the leaky columns
  (asserted by :func:`_assert_honest_frame`), and a test asserts the fence.

* **Stacking redundancy probe** (:func:`stacking_probe`) — an **out-of-fold**
  (unit-grouped) meta-learner over the base rungs. If the stack can't beat its **best
  base member**, the rungs are information-redundant → the ceiling is the *data*,
  confirmed. Reported either way. Stacking lives here **as a probe, not a product** —
  a within-noise bump on the binary task would only muddy the clean F2.7 finding.

**Compute (ADR-010).** Everything here runs CPU-only on the low-end desktop: the
decomposition and the probe use the cheap LightGBM rungs (per-row + temporal-features).
The F2.7 **TCN** rung needs the GPU, so the probe takes its base predictions through a
**seam** — :func:`stacking_probe` accepts extra out-of-fold columns via ``extra_oof``,
so a notebook run can fold the TCN's OOF in later **without a rewrite**. Since F2.7
measured the TCN *below* rung (b), the probe's verdict (redundant vs. not) is already
decidable from the two GBDT rungs; the TCN-included number is an optional refinement.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import GroupKFold

from . import config, features, models, sequence

#: Label-side columns the **upper-bound diagnostic** is allowed to see and the honest
#: path must never see. ``failure_mode`` is the injected-failure label; the derived
#: time-to-failure (:func:`time_to_failure`) is added under this name at fit time.
LEAK_FEATURES: tuple[str, ...] = ("failure_mode", "time_to_failure_h")

#: Right-open time-to-failure buckets (hours) for the decomposition. The canonical label
#: horizon is 168 h (``configs/dataset.json``); buckets get finer near failure, where the
#: degradation ramp (generator ADR-020) is strongest, and coarsen out toward the horizon.
TTF_BUCKETS_H: tuple[float, ...] = (0.0, 6.0, 24.0, 72.0, 168.0, float("inf"))

#: Folds for the out-of-fold stacking probe — unit-grouped so no unit leaks across a fold
#: (the ADR-003 guard holds inside the meta-learner exactly as it does in F2.6's HPO).
STACK_FOLDS: int = 5


# --------------------------------------------------------------------------- #
# Label-side time-to-failure (a *diagnostic*, never a feature)                  #
# --------------------------------------------------------------------------- #


def time_to_failure(readings: pd.DataFrame) -> np.ndarray:
    """Hours from each row to its unit's failure event — **label-side**, diagnostic-only.

    The target ``failure_within_h`` marks the horizon window *before* a failure; per unit
    that window is a contiguous run of positives (verified on the data). The failure
    **event** lands one stride past the last positive row, so time-to-failure at a
    positive row ``t`` is ``event_time - t``. Rows outside any positive window (genuinely
    healthy) get ``+inf``. Aligned to ``readings`` row order.

    This is computed **from the labels** and is used only to *bucket* the decomposition
    and to feed the *fenced* upper-bound — never as an honest feature (the ADR-003
    leakage guard: it would trivially leak the answer).
    """
    for required in (config.TARGET, sequence.GROUP_COLUMN, sequence.TIME_COLUMN):
        if required not in readings.columns:
            raise KeyError(f"time_to_failure needs {required!r}.")

    ttf = np.full(len(readings), np.inf, dtype="float64")
    pos_mask = readings[config.TARGET].to_numpy() == 1
    if not pos_mask.any():
        return ttf

    t = readings[sequence.TIME_COLUMN].to_numpy(dtype="float64")
    units = readings[sequence.GROUP_COLUMN].to_numpy()
    pos_pos = np.flatnonzero(pos_mask)  # positional indices of positive rows

    # One stride = the unit's smallest positive time step (5 min full data / 1 h fixture).
    order = np.lexsort((t, units))
    ts_sorted = t[order]
    u_sorted = units[order]
    same_unit = u_sorted[1:] == u_sorted[:-1]
    steps = ts_sorted[1:][same_unit] - ts_sorted[:-1][same_unit]
    steps = steps[steps > 0]
    stride = float(steps.min()) if len(steps) else 1.0

    # event_time per unit = (max positive timestamp for that unit) + one stride.
    pos_df = pd.DataFrame({"unit": units[pos_pos], "t": t[pos_pos]})
    event_time = pos_df.groupby("unit")["t"].max() + stride
    ttf[pos_pos] = event_time.reindex(units[pos_pos]).to_numpy() - t[pos_pos]
    return ttf


# --------------------------------------------------------------------------- #
# Shared honest base: the per-row + temporal-features LightGBM predictions      #
# --------------------------------------------------------------------------- #


def _assert_honest_frame(X: pd.DataFrame) -> None:
    """Belt-and-braces: the honest path must carry neither target/label nor leak features."""
    features.assert_no_leakage(X)
    leaked = [c for c in LEAK_FEATURES if c in X.columns]
    if leaked:
        raise ValueError(
            f"ceiling: leak feature(s) reached the honest path: {leaked}. "
            "The upper-bound is a fenced diagnostic — never mix it into a reported metric."
        )


@dataclass(frozen=True)
class BaseFrames:
    """The honest feature frames + shared split the F2.8 instruments all reuse.

    ``X_perrow`` / ``X_temporal`` are the two F2.7 LightGBM inputs (signals-only and
    causal-window stats); ``y`` is the binary target; ``train_idx`` / ``test_idx`` are the
    **exact F1** unit-grouped rows; ``groups`` are the row ``unit_id`` (for grouped OOF).
    """

    X_perrow: pd.DataFrame
    X_temporal: pd.DataFrame
    y: np.ndarray
    train_idx: np.ndarray
    test_idx: np.ndarray
    groups: np.ndarray

    @property
    def base_columns(self) -> dict[str, pd.DataFrame]:
        return {"lightgbm_perrow": self.X_perrow, "lightgbm_temporal": self.X_temporal}


def build_base(
    readings: pd.DataFrame, *, seed: int | None = None, window: int = sequence.DEFAULT_WINDOW
) -> BaseFrames:
    """Assemble the honest per-row + temporal frames and the shared F1 split."""
    if seed is None:
        seed = config.DEFAULT_SEED
    train_idx, test_idx = sequence.split_indices(readings, seed=seed)
    X_perrow = features.select_features(readings)
    X_temporal = sequence.temporal_features(readings, window=window)
    _assert_honest_frame(X_perrow)
    _assert_honest_frame(X_temporal)
    y = readings[config.TARGET].astype("int8").to_numpy()
    groups = readings[sequence.GROUP_COLUMN].to_numpy()
    return BaseFrames(
        X_perrow=X_perrow,
        X_temporal=X_temporal,
        y=y,
        train_idx=train_idx,
        test_idx=test_idx,
        groups=groups,
    )


def _fit_predict(
    X: pd.DataFrame, y: np.ndarray, train_idx: np.ndarray, eval_idx: np.ndarray, *, seed: int
) -> np.ndarray:
    """Fit a LightGBM on ``train_idx`` rows, return proba on ``eval_idx`` rows."""
    model = models.build_lightgbm(seed=seed)
    model.fit(X.iloc[train_idx].reset_index(drop=True), pd.Series(y[train_idx]))
    return model.predict_proba(X.iloc[eval_idx].reset_index(drop=True))


# --------------------------------------------------------------------------- #
# (1) Decomposition — where predictability lives                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Slice:
    """One decomposition slice: its label, row count, positive count, held-out AUC."""

    label: str
    n_rows: int
    n_positive: int
    roc_auc: float | None  # None when the slice is single-class (AUC undefined)


@dataclass(frozen=True)
class Decomposition:
    """The honest held-out AUC overall + sliced by TTF horizon and by failure mode."""

    overall: float
    by_horizon: list[Slice]
    by_mode: list[Slice]


def _slice_auc(y_true: np.ndarray, proba: np.ndarray, mask: np.ndarray, label: str) -> Slice:
    """AUC on the ``mask`` subset (None if that subset is single-class)."""
    yt = y_true[mask]
    n_pos = int(yt.sum())
    auc: float | None = None
    if 0 < n_pos < len(yt):
        auc = float(roc_auc_score(yt, proba[mask]))
    return Slice(label=label, n_rows=int(mask.sum()), n_positive=n_pos, roc_auc=auc)


def decompose(base: BaseFrames, readings: pd.DataFrame, *, seed: int) -> Decomposition:
    """Held-out AUC of the honest per-row rung, sliced by TTF horizon and failure mode.

    A **negative** row (no failure ahead) has no time-to-failure, so the horizon buckets
    each pair the *positives* in that TTF band against **all** held-out negatives — the AUC
    then reads "how separable are failures *this close* from healthy rows", which is the
    quantity of interest (near failure it should approach 1.0; far out it should fade).
    The failure-mode slices likewise pair each mode's positives against all negatives.
    """
    test_idx = base.test_idx
    proba = _fit_predict(base.X_perrow, base.y, base.train_idx, test_idx, seed=seed)
    y_test = base.y[test_idx]
    overall = float(roc_auc_score(y_test, proba)) if 0 < y_test.sum() < len(y_test) else float("nan")

    neg = y_test == 0
    ttf_test = time_to_failure(readings)[test_idx]
    mode_test = readings["failure_mode"].to_numpy()[test_idx].astype(str)

    by_horizon: list[Slice] = []
    for lo, hi in zip(TTF_BUCKETS_H[:-1], TTF_BUCKETS_H[1:]):
        band_pos = (y_test == 1) & (ttf_test >= lo) & (ttf_test < hi)
        hi_lab = "inf" if hi == float("inf") else f"{hi:g}"
        by_horizon.append(_slice_auc(y_test, proba, band_pos | neg, f"[{lo:g},{hi_lab}) h"))

    by_mode: list[Slice] = []
    for mode in sorted(m for m in np.unique(mode_test) if m and m != "nan"):
        mode_pos = (y_test == 1) & (mode_test == mode)
        by_mode.append(_slice_auc(y_test, proba, mode_pos | neg, mode))

    return Decomposition(overall=overall, by_horizon=by_horizon, by_mode=by_mode)


# --------------------------------------------------------------------------- #
# (2) Label-leaking upper-bound — the irreducible-error diagnostic (FENCED)     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class UpperBound:
    """The **diagnostic** upper-bound AUC from a model that sees the label-side columns.

    ``leaky`` is intentionally high — it is **not a result**, it bounds the irreducible
    error. ``honest`` is the real per-row AUC (same as :attr:`Decomposition.overall`);
    ``gap`` = leaky − honest is how much room a *better model on the same features* could
    ever have. A small gap ⇒ the honest model is near the information ceiling.
    """

    honest: float
    leaky: float
    gap: float
    leak_features: tuple[str, ...]


def upper_bound(base: BaseFrames, readings: pd.DataFrame, *, seed: int) -> UpperBound:
    """Fit a deliberately label-leaking model to bound the irreducible error.

    The leaky model sees the honest signals **plus** ``failure_mode`` (one-hot) and the
    derived ``time_to_failure_h`` — columns that are only knowable *because* the failure
    occurred. Its AUC is a diagnostic ceiling, **never reported as a result**: it is kept
    in its own field and the honest frames are asserted leak-free upstream.
    """
    honest_proba = _fit_predict(base.X_perrow, base.y, base.train_idx, base.test_idx, seed=seed)
    y_test = base.y[base.test_idx]
    honest = float(roc_auc_score(y_test, honest_proba))

    # Build the leaky frame in isolation — it exists only inside this function.
    leaky = base.X_perrow.copy()
    ttf = time_to_failure(readings)
    leaky["time_to_failure_h"] = np.where(np.isfinite(ttf), ttf, 1e9)  # +inf → a big finite value
    mode = readings["failure_mode"].astype(str).replace("nan", "")
    for m in sorted(u for u in mode.unique() if u):
        leaky[f"failure_mode__{m}"] = (mode == m).astype("int8").to_numpy()

    leaky_model = models.build_lightgbm(seed=seed)
    leaky_model.fit(
        leaky.iloc[base.train_idx].reset_index(drop=True), pd.Series(base.y[base.train_idx])
    )
    leaky_proba = leaky_model.predict_proba(leaky.iloc[base.test_idx].reset_index(drop=True))
    leaky_auc = float(roc_auc_score(y_test, leaky_proba))

    return UpperBound(
        honest=honest,
        leaky=leaky_auc,
        gap=leaky_auc - honest,
        leak_features=LEAK_FEATURES,
    )


# --------------------------------------------------------------------------- #
# (3) Stacking redundancy probe — are the rungs information-redundant?          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StackingProbe:
    """The OOF stacking verdict: does a meta-learner beat its best base member?

    ``base_test`` maps each base rung to its held-out AUC; ``best_base`` is the strongest;
    ``stack`` is the meta-learner's held-out AUC. ``margin`` = stack − best_base. If
    ``margin`` is not clearly positive the rungs are information-**redundant** → the
    ceiling is the *data* (the F2.8 thesis), confirmed. Stacking is a **probe, not a
    product**: reported either way, never shipped as the model.
    """

    base_test: dict[str, float]
    best_base: str
    best_base_auc: float
    stack_auc: float
    margin: float
    beats_best_base: bool
    oof_columns: list[str]


def _oof_predictions(
    X: pd.DataFrame, y: np.ndarray, train_idx: np.ndarray, groups: np.ndarray, *, seed: int
) -> np.ndarray:
    """Unit-grouped out-of-fold proba for the **train** rows (no row scored by its own fit).

    ``GroupKFold`` over the training units only — the meta-learner is fit on predictions no
    base model saw the target of, so the stack can't launder train-set overfitting into an
    inflated meta score (and no unit leaks across a fold, ADR-003).
    """
    oof = np.full(len(train_idx), np.nan, dtype="float64")
    g = groups[train_idx]
    n_splits = min(STACK_FOLDS, len(np.unique(g)))
    gkf = GroupKFold(n_splits=n_splits)
    Xtr = X.iloc[train_idx].reset_index(drop=True)
    ytr = y[train_idx]
    for inner_tr, inner_val in gkf.split(Xtr, ytr, g):
        if len(np.unique(ytr[inner_tr])) < 2:
            continue  # a degenerate fold can't fit; leave those OOF rows NaN → dropped
        model = models.build_lightgbm(seed=seed)
        model.fit(Xtr.iloc[inner_tr].reset_index(drop=True), pd.Series(ytr[inner_tr]))
        oof[inner_val] = model.predict_proba(Xtr.iloc[inner_val].reset_index(drop=True))
    return oof


def stacking_probe(
    base: BaseFrames,
    *,
    seed: int,
    extra_oof: dict[str, tuple[np.ndarray, np.ndarray]] | None = None,
) -> StackingProbe:
    """OOF meta-learner over the base rungs — does stacking beat the best single rung?

    Base rungs are the two honest LightGBM frames (per-row + temporal-features). The
    meta-learner is a plain :class:`LogisticRegression` over **out-of-fold** base proba on
    the train rows (so it never sees a base model's in-sample fit), then evaluated on the
    held-out test rows against base models refit on all train rows.

    ``extra_oof`` is the **TCN seam**: a mapping ``name -> (oof_train, proba_test)`` of a
    rung whose predictions were produced elsewhere (e.g. the GPU TCN from a notebook). Its
    OOF/test columns are folded into the stack with no rewrite; ``oof_train`` must align to
    ``base.train_idx`` and ``proba_test`` to ``base.test_idx``.
    """
    train_idx, test_idx = base.train_idx, base.test_idx
    y_train, y_test = base.y[train_idx], base.y[test_idx]

    oof_cols: dict[str, np.ndarray] = {}
    test_cols: dict[str, np.ndarray] = {}
    base_test: dict[str, float] = {}
    for name, X in base.base_columns.items():
        oof_cols[name] = _oof_predictions(X, base.y, train_idx, base.groups, seed=seed)
        test_cols[name] = _fit_predict(X, base.y, train_idx, test_idx, seed=seed)
        base_test[name] = float(roc_auc_score(y_test, test_cols[name]))

    for name, (oof_tr, proba_te) in (extra_oof or {}).items():
        oof_tr = np.asarray(oof_tr, dtype="float64")
        proba_te = np.asarray(proba_te, dtype="float64")
        if oof_tr.shape[0] != len(train_idx) or proba_te.shape[0] != len(test_idx):
            raise ValueError(
                f"extra_oof[{name!r}] misaligned: expected "
                f"({len(train_idx)},)/({len(test_idx)},), got "
                f"{oof_tr.shape}/{proba_te.shape}."
            )
        oof_cols[name] = oof_tr
        test_cols[name] = proba_te
        base_test[name] = float(roc_auc_score(y_test, proba_te))

    names = list(oof_cols)
    Z_train = np.column_stack([oof_cols[n] for n in names])
    Z_test = np.column_stack([test_cols[n] for n in names])
    keep = np.isfinite(Z_train).all(axis=1)  # drop rows with a NaN OOF (degenerate fold)

    meta = LogisticRegression(max_iter=1000, class_weight="balanced")
    meta.fit(Z_train[keep], y_train[keep])
    stack_proba = meta.predict_proba(Z_test)[:, 1]
    stack_auc = float(roc_auc_score(y_test, stack_proba))

    best_base = max(base_test, key=base_test.get)
    best_auc = base_test[best_base]
    margin = stack_auc - best_auc
    return StackingProbe(
        base_test=base_test,
        best_base=best_base,
        best_base_auc=best_auc,
        stack_auc=stack_auc,
        margin=margin,
        beats_best_base=margin > 0.0,
        oof_columns=names,
    )


# --------------------------------------------------------------------------- #
# The capstone: run all three, report the verdict                              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CeilingReport:
    """The F2.8 capstone: decomposition + fenced upper-bound + stacking probe verdict."""

    decomposition: Decomposition
    upper_bound: UpperBound
    stacking: StackingProbe
    seed: int
    window: int

    @property
    def ceiling_is_data(self) -> bool:
        """The thesis: rungs redundant (stack doesn't beat best base) ⇒ ceiling is the data."""
        return not self.stacking.beats_best_base


def characterize(
    readings: pd.DataFrame,
    *,
    seed: int | None = None,
    window: int = sequence.DEFAULT_WINDOW,
    extra_oof: dict[str, tuple[np.ndarray, np.ndarray]] | None = None,
) -> CeilingReport:
    """Run the three F2.8 instruments on the shared honest base and return the report."""
    if seed is None:
        seed = config.DEFAULT_SEED
    base = build_base(readings, seed=seed, window=window)
    if len(np.unique(base.y[base.test_idx])) < 2:
        from .train import DegenerateSplit

        raise DegenerateSplit(
            "the held-out test set has a single class; AUC is undefined "
            "(only an issue on the reduced smoke fixture; the full dataset is fine)."
        )
    return CeilingReport(
        decomposition=decompose(base, readings, seed=seed),
        upper_bound=upper_bound(base, readings, seed=seed),
        stacking=stacking_probe(base, seed=seed, extra_oof=extra_oof),
        seed=seed,
        window=window,
    )


def format_report(report: CeilingReport) -> str:
    """A compact, human-readable capstone report for the CLI."""
    d, ub, sp = report.decomposition, report.upper_bound, report.stacking
    lines = [f"Ceiling characterization ({config.PRIMARY_METRIC}), F1 split / seed {report.seed}:"]
    lines.append(f"  overall (per-row honest): {d.overall:.4f}")

    lines.append("  by time-to-failure horizon (positives in band vs. all healthy):")
    for s in d.by_horizon:
        auc = "n/a  " if s.roc_auc is None else f"{s.roc_auc:.4f}"
        lines.append(f"    {s.label:<14} {auc}  (pos={s.n_positive})")

    lines.append("  by failure mode (mode's positives vs. all healthy):")
    for s in d.by_mode:
        auc = "n/a  " if s.roc_auc is None else f"{s.roc_auc:.4f}"
        lines.append(f"    {s.label:<20} {auc}  (pos={s.n_positive})")

    lines.append(
        f"  upper-bound (DIAGNOSTIC, leaks {', '.join(ub.leak_features)}): "
        f"{ub.leaky:.4f}  → gap over honest {ub.gap:+.4f}"
    )

    lines.append("  stacking redundancy probe (OOF meta-learner over the rungs):")
    for name, auc in sorted(sp.base_test.items(), key=lambda kv: kv[1], reverse=True):
        star = "  <- best base" if name == sp.best_base else ""
        lines.append(f"    {name:<20} {auc:.4f}{star}")
    lines.append(f"    stack                {sp.stack_auc:.4f}  ({sp.margin:+.4f} vs. best base)")
    verdict = "beats" if sp.beats_best_base else "does NOT beat"
    lines.append(f"  → the stack {verdict} its best member.")
    lines.append(
        "  VERDICT: "
        + (
            "rungs are information-redundant → the ceiling is the DATA (thesis confirmed)."
            if report.ceiling_is_data
            else "the stack adds signal → the rungs are NOT fully redundant (thesis not confirmed)."
        )
    )
    return "\n".join(lines)
