"""Generate-your-own-data — the bounded generation spec, the caps, and the roll-up (F14a).

This is the **pure core** of the generate-a-fleet feature: what a user is allowed to ask
for (:class:`GenerationSpec` + the caps), how that becomes a run of the companion
generator, and how per-row failure probabilities become **one risk score per vehicle**.

Two things it deliberately is *not*:

* **It is not the API.** :func:`run_generation` is executed by the **worker** — a separate
  deployable unit (a Cloud Run Job; ADR-026 / decision S2), never inside a request. The
  serving process imports this module only for :func:`GenerationSpec.validate` (to reject a
  request cheaply) and :func:`roll_up` (to score what the worker stored); it never calls
  :func:`run_generation`. The generator import is **lazy**, inside that function, so the
  API container does not even need the ``[generate]`` extra.
* **It is not a training path.** The generated dataset is scored by whatever model is
  *currently promoted* — on the hosted demo that is the fixture-trained demo model
  (``demo=fixture``, ADR-014), and the report says so.

The caps (:data:`MAX_UNITS` / :data:`MAX_DAYS` / :data:`MAX_UNIT_DAYS`) exist because the
whole demo must fit a **free** Cloud Run container and a **free** 0.5 GB Neon Postgres
(decision G1). They are derived, not guessed — see ADR-026.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from . import config, features

# --- what the demo is allowed to generate (the $0 envelope, decision G1) ------

#: Fixed sampling stride. Matches ``configs/dataset.json`` — the demo generates a *slice*
#: of the canonical recipe, not a different dataset (ADR-001). 12 readings per hour.
RESOLUTION: str = "5min"
ROWS_PER_UNIT_DAY: int = 288  # 24 h × 12 readings/h

MIN_UNITS: int = 3
MAX_UNITS: int = 30
MIN_DAYS: int = 1
MAX_DAYS: int = 14

#: The **binding** cap: fleet size × window, so a user can trade a bigger fleet for a
#: shorter window and vice-versa. 200 unit-days = 57,600 readings ≈ **25 MB** stored (at a
#: measured ~432 B/row including the index) — ~5% of Neon's free 0.5 GB per run.
#:
#: It is a **storage** cap, and it is worth being clear that it is not a *time* cap: the
#: measured cost of a full-size run is ~1 s to generate and ~0.2 s to score, nowhere near
#: any timeout. Generation is out-of-process because that is the right shape (S2), not
#: because it is slow — and the thing that would actually break this demo is a public
#: endpoint quietly filling a free-tier database (ADR-026).
MAX_UNIT_DAYS: int = 200

DEFAULT_UNITS: int = 20
DEFAULT_DAYS: int = 7

#: Total rows retained across *all* runs before the oldest are pruned. At the measured
#: ~432 B/row this is ~86 MB — under a fifth of the free 0.5 GB, so the prediction log
#: (F7), Postgres's own overhead, and a burst of concurrent runs all still fit. A public
#: demo with no retention policy is a database that fills up and takes the *rest* of the
#: demo down with it.
MAX_TOTAL_STORED_ROWS: int = 200_000

# --- the per-vehicle roll-up (ADR-026) ----------------------------------------

#: The model answers *per row*: "does this unit fail within 168 h of **this reading**?".
#: A vehicle has thousands of rows, so the report needs one number per vehicle — and the
#: choice of statistic is not cosmetic. **Measured** (ADR-026): the two obvious candidates
#: both lose. ``max`` over the unit's rows is a **one-row statistic** and the generator
#: deliberately injects outliers and sensor faults, so a single spurious spike flags a
#: healthy vehicle (unit ROC-AUC 0.908, ±0.118 — and healthy units peak near 1.0 too).
#: The share of rows above a threshold *dilutes*: a failure ramp occupies a small slice of
#: a 7-day window (0.835). What actually separates a degrading vehicle from a healthy one
#: is a **sustained** run of high scores — which is what a real degradation ramp produces
#: and an injected outlier cannot. So: take a rolling mean over one hour, then its peak.
#: Measured **0.970** unit ROC-AUC (±0.022) with the widest healthy/failing separation.
ROLLUP_WINDOW_H: float = 1.0
ROLLUP_WINDOW_ROWS: int = 12  # 1 h at the fixed 5-minute stride

#: A vehicle is *flagged* above this sustained risk. Chosen from the measured
#: recall/precision knee (ADR-026), keeping the recall bias the plan asks for: a missed
#: failure costs more than an unnecessary inspection.
FLAG_THRESHOLD: float = 0.70

#: The per-row threshold behind the reported ``high_risk_share`` (the same 50% band the
#: F8 upload summary uses — kept identical so the two demo surfaces agree).
HIGH_RISK_ROW_THRESHOLD: float = 0.5


class CapExceeded(ValueError):
    """The requested generation is outside the demo's bounds (a 4xx, never a 500)."""


@dataclass(frozen=True)
class GenerationSpec:
    """What the user asked the forge to generate — validated against the caps."""

    n_units: int = DEFAULT_UNITS
    days: int = DEFAULT_DAYS
    seed: int = config.DEFAULT_SEED

    @property
    def unit_days(self) -> int:
        return self.n_units * self.days

    @property
    def expected_rows(self) -> int:
        """Rows the forge will emit — exact, since the fleet size is pinned (sd = 0)."""
        return self.unit_days * ROWS_PER_UNIT_DAY

    def validate(self) -> GenerationSpec:
        """Raise :class:`CapExceeded` if the request is out of bounds; return self.

        Every message names the bound *and* the value, so the UI can show it verbatim.
        """
        if not MIN_UNITS <= self.n_units <= MAX_UNITS:
            raise CapExceeded(
                f"fleet size must be between {MIN_UNITS} and {MAX_UNITS} vehicles "
                f"(got {self.n_units})."
            )
        if not MIN_DAYS <= self.days <= MAX_DAYS:
            raise CapExceeded(
                f"window must be between {MIN_DAYS} and {MAX_DAYS} days (got {self.days})."
            )
        if self.unit_days > MAX_UNIT_DAYS:
            raise CapExceeded(
                f"fleet size × window must be at most {MAX_UNIT_DAYS} unit-days "
                f"(got {self.n_units} × {self.days} = {self.unit_days}); that is "
                f"{self.expected_rows:,} readings, over the "
                f"{MAX_UNIT_DAYS * ROWS_PER_UNIT_DAY:,}-reading cap this free-tier demo "
                f"stores. Shrink the fleet or the window."
            )
        if self.seed < 0:
            raise CapExceeded(f"seed must be non-negative (got {self.seed}).")
        return self


def build_forge_config(spec: GenerationSpec):
    """Turn a :class:`GenerationSpec` into a bounded ``can-telemetry-forge`` config.

    Starts from the **canonical** ``configs/dataset.json`` recipe (ADR-001 — one dataset
    definition across machines) and overrides only what the demo bounds: the window, the
    stride, and a fleet cut down to ``n_units``. The fleet's per-contract size variance is
    pinned to **zero** so the user gets *exactly* the fleet size they asked for — the
    generator otherwise draws the count around the requested one, which would make the
    caps (and the storage arithmetic behind them) approximate.

    Units are spread round-robin across the canonical regions, so a small demo fleet still
    carries the regional climate variety the report will lean on later (F12).
    """
    from can_telemetry_forge.config import config_from_dict, load_config

    # NOT `config.DATASET_CONFIG` directly: the worker runs the package **installed**, where
    # that path points into site-packages (see `config.dataset_config_path`).
    canonical = load_config(str(config.dataset_config_path()))
    regions = canonical.fleet.regions
    n_contracts = min(len(regions), spec.n_units)

    # Spread n_units over the regions with no remainder left behind.
    sizes = [spec.n_units // n_contracts] * n_contracts
    for i in range(spec.n_units % n_contracts):
        sizes[i] += 1

    contracts = [
        {
            "id": f"demo_{regions[i].id}",
            "label": f"Demo — {regions[i].id}",
            "region_id": regions[i].id,
            "units": sizes[i],
            "duty_bias": 1.0,
        }
        for i in range(n_contracts)
    ]
    return config_from_dict(
        {
            "seed": spec.seed,
            "days": spec.days,
            "resolution": RESOLUTION,
            "failure_horizon_h": canonical.failure_horizon_h,
            "fleet": {"contracts": contracts, "units_per_contract_sd_frac": 0.0},
        }
    )


def run_generation(spec: GenerationSpec) -> pd.DataFrame:
    """Run the forge for ``spec`` and return the readings — **worker-side only**.

    Imports the generator lazily: the serving container never reaches this function (the
    API only *enqueues*, decision S2), so it must not pay for the ``[generate]`` extra at
    import time.

    The returned frame is sorted by ``(unit_id, t_index)`` — the roll-up's rolling window
    is only meaningful in time order, so the ordering is established once, here, at the
    source, rather than trusted downstream.
    """
    from can_telemetry_forge.sim.simulate import simulate

    readings = simulate(build_forge_config(spec).validate()).readings
    return readings.sort_values(["unit_id", "t_index"]).reset_index(drop=True)


def stored_columns() -> list[str]:
    """The columns the store keeps per generated reading (signals + the time key).

    The label columns (``failure_within_h`` / ``failure_mode`` / the anomaly bookkeeping)
    are **deliberately dropped**: the demo scores this data with a model, it does not grade
    it, and keeping the answer next to the question is how a leak starts (ADR-003).
    """
    return ["unit_id", "t_index", *features.FEATURE_COLUMNS]


@dataclass(frozen=True)
class UnitRisk:
    """One vehicle's line in the report."""

    unit_id: str
    n_rows: int
    risk: float              # the sustained-risk roll-up (the ranked/flagged number)
    peak: float              # max single-row probability — shown, never ranked on
    high_risk_share: float   # share of rows ≥ HIGH_RISK_ROW_THRESHOLD
    flagged: bool


def roll_up(frame: pd.DataFrame, proba: np.ndarray) -> list[UnitRisk]:
    """Per-row probabilities → **one risk score per vehicle**, ranked riskiest first.

    ``frame`` must carry ``unit_id`` and ``t_index`` and be aligned with ``proba``. The
    risk score is the peak of a one-hour rolling mean (see :data:`ROLLUP_WINDOW_ROWS` for
    why that, and not ``max`` or a threshold share). ``peak`` and ``high_risk_share`` ride
    along so the report can *show* the raw numbers the rule deliberately does not rank on.
    """
    if len(frame) != len(proba):
        raise ValueError(
            f"roll-up misalignment: {len(frame)} rows but {len(proba)} probabilities."
        )
    scored = frame[["unit_id", "t_index"]].copy()
    scored["p"] = np.asarray(proba, dtype=float)
    scored = scored.sort_values(["unit_id", "t_index"])

    out: list[UnitRisk] = []
    for unit_id, g in scored.groupby("unit_id", sort=True):
        p = g["p"]
        sustained = float(
            p.rolling(ROLLUP_WINDOW_ROWS, min_periods=1).mean().max()
        )
        out.append(
            UnitRisk(
                unit_id=str(unit_id),
                n_rows=int(len(p)),
                risk=sustained,
                peak=float(p.max()),
                high_risk_share=float((p >= HIGH_RISK_ROW_THRESHOLD).mean()),
                flagged=sustained >= FLAG_THRESHOLD,
            )
        )
    out.sort(key=lambda u: u.risk, reverse=True)
    return out
