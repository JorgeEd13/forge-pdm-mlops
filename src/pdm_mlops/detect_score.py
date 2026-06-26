"""Score the detection ladder against the generator's ground-truth outlier labels.

This is the *honesty harness* for F2.5. The detectors in :mod:`pdm_mlops.detect` run
on signals only and emit a label-free suspicion score; **here** — and only here — the
generator's ``is_outlier`` / ``anomaly_type`` labels are read, to **grade** each rung:

* an overall **ROC-AUC** and **average precision** (AP) of each rung's score vs.
  ``is_outlier`` (the generator's "this row is a value-level outlier" flag);
* a per-:data:`anomaly_type`-family **recall** at a fixed alarm budget, so we can see
  *which* defect families each rung actually catches (joint vs. stuck vs. drift …);
* whether the **autoencoder earns its place** — does it measurably beat the cheap rungs
  on the *subtle* families? — reported plainly either way.

The labels never flow back into a detector or a model feature (the F1 leakage guard,
ADR-003, stays sacred). They live exactly one place: this scoring module.

``is_outlier`` is the generator's value-level outlier flag; it covers
``obvious_outlier``, ``joint_outlier``, ``sensor_stuck``, ``sensor_drift`` and the
value-corrupting CAN-frame faults, but **not** ``sensor_dropout`` (NaN injection,
indistinguishable from era-NULL by design). So a perfect detector does not reach
recall 1.0 on every family — the harness reports the families honestly rather than
hiding the ones nothing can catch.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score

from . import detect

#: The generator's row-level outlier flag — the primary binary ground truth.
LABEL_OUTLIER: str = "is_outlier"
#: The categorical defect family per row ("" / NaN for normal rows).
LABEL_FAMILY: str = "anomaly_type"

#: Families broken out in the recall table, grouped obvious vs. subtle. ``obvious_*``
#: is a single signal out of range; the rest are the subtle ones F2.5 exists to catch.
OBVIOUS_FAMILIES: tuple[str, ...] = ("obvious_outlier",)
SUBTLE_FAMILIES: tuple[str, ...] = ("joint_outlier", "sensor_stuck", "sensor_drift")

#: Fraction of rows allowed to alarm when measuring per-family recall — a fixed budget
#: so detectors are compared at the same alarm rate (otherwise a flag-everything rung
#: would "win" on recall). 2 % ≈ the planted-outlier base rate.
ALARM_BUDGET: float = 0.02


@dataclass(frozen=True)
class RungScore:
    """One detector's grades vs. ground truth."""

    name: str
    roc_auc: float
    average_precision: float
    #: family → recall at :data:`ALARM_BUDGET`.
    family_recall: dict[str, float]


@dataclass(frozen=True)
class LadderScore:
    """The whole ladder's scored comparison + the autoencoder verdict."""

    rungs: list[RungScore]
    #: families present in the scored data, in report order (obvious then subtle).
    families: list[str]
    #: did the autoencoder beat the best cheap rung on mean *subtle* recall? ``None``
    #: if the autoencoder was not in the ladder.
    autoencoder_earns_place: bool | None
    autoencoder_subtle_recall: float | None
    best_cheap_subtle_recall: float | None


def _alarm_set(scores: np.ndarray, budget: float) -> np.ndarray:
    """The boolean "alarmed" mask of (at most) the top-``budget`` fraction of rows.

    Tie-aware: a sparse/binary detector (mostly zeros, a few ones) must NOT alarm every
    row just because the budget quantile lands on its zero floor. We take the top
    ``budget·n`` rows by score, but if the threshold value is tied across more rows than
    the budget allows, we keep only the rows **strictly above** it — so a detector that
    flags 0.6 % of rows is credited with exactly those, not the whole dataset.
    """
    n = len(scores)
    k = max(1, int(round(budget * n)))
    kth = np.partition(scores, n - k)[n - k]  # value at the budget boundary
    if (scores == kth).sum() > k - (scores > kth).sum():
        # ties overflow the budget at the boundary → only strictly-greater rows alarm
        return scores > kth
    return scores >= kth


def _recall_at_budget(scores: np.ndarray, family_mask: np.ndarray, budget: float) -> float:
    """Recall on ``family_mask`` rows when (at most) the top-``budget`` fraction alarms."""
    if family_mask.sum() == 0:
        return float("nan")
    flagged = _alarm_set(scores, budget)
    return float((flagged & family_mask).sum() / family_mask.sum())


def score_rung(
    name: str,
    scores: np.ndarray,
    labels: pd.DataFrame,
    families: list[str],
    *,
    budget: float = ALARM_BUDGET,
) -> RungScore:
    """Grade one rung's suspicion scores against the labels."""
    y = labels[LABEL_OUTLIER].to_numpy().astype(bool)
    fam = labels[LABEL_FAMILY].astype(str).to_numpy()
    auc = float(roc_auc_score(y, scores)) if y.any() and (~y).any() else float("nan")
    ap = float(average_precision_score(y, scores)) if y.any() else float("nan")
    recalls = {f: _recall_at_budget(scores, fam == f, budget) for f in families}
    return RungScore(name=name, roc_auc=auc, average_precision=ap, family_recall=recalls)


def score_ladder(
    readings: pd.DataFrame,
    *,
    seed: int | None = None,
    include_autoencoder: bool = True,
    budget: float = ALARM_BUDGET,
) -> LadderScore:
    """Fit + score every rung against ground truth; decide if the AE earns its place.

    ``readings`` must carry the ground-truth label columns (the fixture does). The
    detectors themselves still see signals only — :func:`detect.fit_score_all` never
    passes them the labels.
    """
    for required in (LABEL_OUTLIER, LABEL_FAMILY):
        if required not in readings.columns:
            raise KeyError(
                f"scoring needs the ground-truth column {required!r}; pass a frame that "
                "carries the generator's labels (the fixture does)."
            )

    fam_present = set(readings[LABEL_FAMILY].astype(str).unique())
    families = [f for f in (*OBVIOUS_FAMILIES, *SUBTLE_FAMILIES) if f in fam_present]

    scores = detect.fit_score_all(
        readings, seed=seed, include_autoencoder=include_autoencoder
    )
    labels = readings[[LABEL_OUTLIER, LABEL_FAMILY]]
    rungs = [score_rung(name, s, labels, families, budget=budget) for name, s in scores.items()]

    # Does the autoencoder beat the best *cheap* rung on mean subtle recall?
    subtle = [f for f in families if f in SUBTLE_FAMILIES]
    ae_earns: bool | None = None
    ae_subtle: float | None = None
    best_cheap_subtle: float | None = None
    if include_autoencoder and subtle:
        def mean_subtle(rs: RungScore) -> float:
            vals = [rs.family_recall[f] for f in subtle if not np.isnan(rs.family_recall[f])]
            return float(np.mean(vals)) if vals else float("nan")

        by_name = {r.name: r for r in rungs}
        if "autoencoder" in by_name:
            ae_subtle = mean_subtle(by_name["autoencoder"])
            cheap = [mean_subtle(by_name[n]) for n in by_name if n != "autoencoder"]
            cheap = [c for c in cheap if not np.isnan(c)]
            best_cheap_subtle = max(cheap) if cheap else float("nan")
            if not np.isnan(ae_subtle) and not np.isnan(best_cheap_subtle):
                ae_earns = ae_subtle > best_cheap_subtle

    return LadderScore(
        rungs=rungs,
        families=families,
        autoencoder_earns_place=ae_earns,
        autoencoder_subtle_recall=ae_subtle,
        best_cheap_subtle_recall=best_cheap_subtle,
    )


def format_ladder_score(score: LadderScore) -> str:
    """A compact, honest report table for the CLI / docs."""
    lines: list[str] = []
    lines.append("Detection ladder — scored vs. ground truth (is_outlier / anomaly_type):")
    head = f"  {'rung':<14}{'ROC-AUC':>9}{'AP':>8}   " + "".join(
        f"{f.replace('_outlier','').replace('sensor_',''):>10}" for f in score.families
    )
    lines.append(head)
    for r in score.rungs:
        fam = "".join(f"{r.family_recall[f]:>10.2f}" for f in score.families)
        lines.append(f"  {r.name:<14}{r.roc_auc:>9.3f}{r.average_precision:>8.3f}   {fam}")
    lines.append("  (family columns = recall at a fixed top-2% alarm budget)")
    if score.autoencoder_earns_place is not None:
        verdict = "YES" if score.autoencoder_earns_place else "no"
        lines.append(
            f"  Autoencoder earns its place on subtle recall? {verdict} "
            f"(AE={score.autoencoder_subtle_recall:.2f} vs. best cheap "
            f"{score.best_cheap_subtle_recall:.2f})."
        )
    return "\n".join(lines)
