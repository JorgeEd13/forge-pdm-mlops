"""Drift monitoring — an Evidently report + a drift *decision* (F5).

The marquee loop needs one honest question answered: **has the incoming data drifted
far enough from what the production model was trained on that we should retrain?** This
module answers it in two layers:

* **The report (`drift_report`).** Evidently's ``DataDriftPreset`` over the model's
  input signals (:data:`features.FEATURE_COLUMNS`) — per-feature statistical drift
  (Evidently picks the test per column: K-S / Wasserstein for numerics) plus the share
  of features that drifted. The raw Evidently object is available for the HTML artifact;
  we distil it into a small, JSON-serialisable :class:`DriftReport`.

* **The decision (`detect_drift`).** A single policy knob turns that share into a
  boolean *retrain trigger* (ADR-013): drift is declared when the share of drifted
  features reaches :data:`config.DRIFT_SHARE_THRESHOLD`. A share threshold (not
  "any one feature drifted") because a single column tripping on noise should not
  trigger a retrain — the loop should fire on a *distribution* shift, which the
  ``season`` stimulus produces across several correlated signals at once.

**The stimulus (the two repos, one story).** The reference is the data the current
production model trained on (no season); the current window is the *same* canonical
dataset regenerated under the generator's ``season`` knob (``"heatwave"`` by default —
:data:`config.DRIFT_SEASON`), which shifts the thermal signals. So the drift is a real,
controllable distribution shift produced by the companion generator, not a mock.

Evidently lives in the optional ``[ops]`` extra and is imported lazily, so importing this
module (and the CLI wiring it) stays light; only actually building a report needs it.
Frames are injectable so tests run offline on the committed fixture with a synthetic
shift, never touching the generator.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import config, data, features


@dataclass(frozen=True)
class DriftReport:
    """A distilled, JSON-serialisable drift result over the model's input signals.

    ``drifted`` is the decision (the share of drifted features reached the threshold);
    ``share_drifted`` / ``n_drifted`` / ``n_features`` are the evidence; ``by_feature``
    maps each signal to whether *it* drifted; ``threshold`` records the policy the
    decision was made under (auditable). The full Evidently object is intentionally not
    carried here (it is not JSON-friendly) — :func:`drift_report` returns it separately
    when a caller wants the HTML artifact.
    """

    drifted: bool
    share_drifted: float
    n_drifted: int
    n_features: int
    threshold: float
    by_feature: dict[str, bool] = field(default_factory=dict)

    def summary(self) -> str:
        head = "DRIFT" if self.drifted else "stable"
        drifted_cols = sorted(c for c, d in self.by_feature.items() if d)
        cols = f" ({', '.join(drifted_cols)})" if drifted_cols else ""
        return (
            f"[{head}] {self.n_drifted}/{self.n_features} features drifted "
            f"= {self.share_drifted:.2f} (threshold {self.threshold:.2f}){cols}"
        )


def _feature_frame(readings: pd.DataFrame) -> pd.DataFrame:
    """Project readings onto the model's input signals (era-NULL preserved).

    Drift is monitored on exactly the columns the model consumes — reusing
    :func:`features.select_features` keeps the monitored surface identical to the
    trained surface (and re-runs the leakage guard for free).
    """
    return features.select_features(readings)


def drift_report(reference: pd.DataFrame, current: pd.DataFrame):
    """Run Evidently's data-drift preset over the feature signals.

    Returns ``(DriftReport, evidently_report)`` — the distilled decision plus the raw
    Evidently object (for an HTML artifact / the flow's run record). Both frames are
    projected onto :data:`features.FEATURE_COLUMNS` first, so the report is over exactly
    the model's inputs. Evidently is imported here (the ``[ops]`` extra) so the module
    imports without it.
    """
    from evidently.metric_preset import DataDriftPreset
    from evidently.report import Report

    ref = _feature_frame(reference)
    cur = _feature_frame(current)

    report = Report(metrics=[DataDriftPreset()])
    report.run(reference_data=ref, current_data=cur)
    result = report.as_dict()

    drift = _distil(result)
    return drift, report


def _distil(evidently_result: dict) -> DriftReport:
    """Turn Evidently's ``as_dict()`` into a :class:`DriftReport` under our policy.

    Evidently reports its own dataset-drift boolean at its default threshold; we ignore
    that and apply **our** :data:`config.DRIFT_SHARE_THRESHOLD` to the per-feature
    drift flags, so the retrain trigger policy is ours and stated in one place (ADR-013).
    """
    drift_metric = next(
        m for m in evidently_result["metrics"] if m["metric"] == "DataDriftTable"
    )["result"]

    by_feature = {
        col: bool(info["drift_detected"])
        for col, info in drift_metric["drift_by_columns"].items()
        if col in features.FEATURE_COLUMNS
    }
    n_features = len(by_feature)
    n_drifted = sum(by_feature.values())
    share = n_drifted / n_features if n_features else 0.0
    threshold = config.DRIFT_SHARE_THRESHOLD
    return DriftReport(
        drifted=share >= threshold,
        share_drifted=share,
        n_drifted=n_drifted,
        n_features=n_features,
        threshold=threshold,
        by_feature=by_feature,
    )


def detect_drift(
    *,
    season: str | None = None,
    reference: pd.DataFrame | None = None,
    current: pd.DataFrame | None = None,
) -> DriftReport:
    """Decide whether the ``season``-shifted data has drifted from the baseline.

    The high-level entry the flow calls. With no injected frames it regenerates the
    **baseline** (no season) and the **shifted** window (``season`` — defaults to
    :data:`config.DRIFT_SEASON`) from the same canonical config, so the drift is the
    generator's controllable distribution shift (the two-repo story). Tests inject
    ``reference``/``current`` to run offline on the fixture with a synthetic shift.

    Returns only the distilled :class:`DriftReport` (the decision + evidence); callers
    wanting the HTML artifact use :func:`drift_report` directly.
    """
    if season is None:
        season = config.DRIFT_SEASON
    if reference is None:
        reference = data.load_readings()
    if current is None:
        current = data.load_readings(season=season)

    report, _ = drift_report(reference, current)
    return report
