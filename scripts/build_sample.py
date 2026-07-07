"""Regenerate the committed offline **smoke fixture** (``data/sample_readings.parquet``).

This bakes a tiny, reduced slice of the generator's ``readings`` table into the
repo so ``clone && pytest`` and CI run fully offline (ADR-001). It is **not** a
training set — the real pipeline always regenerates the *full* dataset from
``configs/dataset.json`` (the cross-machine source of truth). To keep the fixture
honestly representative, it is built by *reducing the very same canonical config*
(fewer units, a coarser stride), never an independently-parameterized run.

**Multi-mode coverage (ADR-019).** The demo model baked into the hosted image trains
on THIS fixture, so the fixture must contain every failure mode the generator models
(``overheat`` / ``oil_starve`` / ``bearing``) — otherwise the demo model silently
learns only the mode that happened to survive the reduction, and the ``/demo`` presets
for the missing modes score ~0. The unit sample is therefore **stratified by event
mode** (a fixed quota per mode + some healthy units), not just by vehicle class, so
all three signatures are always present. This does not touch the full dataset or the
reported metric — it only makes the OFFLINE slice exercise every code path (rule 3).
The full 90-day window is kept (unreduced), so each mode's pre-event degradation ramp
is the real full-length one.

Run from the repo root after ``pip install -e '.[generate]'``::

    python scripts/build_sample.py

Deterministic: same generator version + canonical config → byte-identical fixture.
"""

from __future__ import annotations

from pathlib import Path

from can_telemetry_forge.config import load_config
from can_telemetry_forge.labels import FAILURE_MODES
from can_telemetry_forge.sim.simulate import simulate

# The canonical training-dataset spec the full pipeline uses — the fixture is a
# strict reduction of THIS, so it can never silently diverge from the real data.
DATASET_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "dataset.json"

# The fixture REDUCES the canonical config on two axes (documented so the reduction is
# explicit + reproducible), preserving the fleet's structure (class mix, era-NULL
# missingness, ALL failure modes) while staying well under ~1 MB. The 90-day window is
# NOT reduced — full-length degradation ramps make the demo model see real signatures.
TIME_STRIDE = 30           # keep every 30th 5-min sample → ~2.5-h cadence on disk
UNITS_PER_MODE = 8         # units to keep per failure mode (overheat/oil_starve/bearing)
HEALTHY_UNITS = 10         # never-fail units, for the negative class + class spread

# Columns the modelling layer (F1/F2) actually consumes: the J1939 signals + keys
# + the labels. Drop the wide raw frame/anomaly bookkeeping the model never reads
# (kept recoverable in the full dataset, just not needed in the offline slice).
KEEP_COLUMNS = [
    "unit_id",
    "timestamp_h",
    "engine_speed_rpm",
    "coolant_temp_c",
    "oil_pressure_kpa",
    "engine_load_pct",
    "fuel_rate_lph",
    "boost_pressure_kpa",
    "egt_c",
    "def_level_pct",
    "vibration_mms",
    "failure_within_h",
    "failure_mode",
    "anomaly_type",
    "is_outlier",
]


def _unit_event_mode(readings) -> "dict[str, str]":
    """Map each ``unit_id`` → its failure mode (``""`` if the unit never fails).

    A unit fails in at most one mode (the earliest-sampled event wins, see
    ``labels.failure.derive_unit_labels``), so the mode on any of its
    ``failure_within_h == 1`` rows is the unit's event mode.
    """
    failing = readings.loc[readings["failure_within_h"] == 1, ["unit_id", "failure_mode"]]
    by_unit = failing.groupby("unit_id")["failure_mode"].first().astype(str)
    modes = dict.fromkeys(readings["unit_id"].unique(), "")
    modes.update(by_unit.to_dict())
    return modes


def _mode_stratified_units(readings, units, seed: int) -> list[str]:
    """Pick units so EVERY failure mode is represented, spread across vehicle classes.

    Stratifies by *event mode* first — a fixed :data:`UNITS_PER_MODE` quota per mode in
    :data:`FAILURE_MODES` plus :data:`HEALTHY_UNITS` never-fail units — and, within each
    stratum, spreads the pick across vehicle classes so the class/era-NULL structure is
    preserved. Deterministic (seeded), so the fixture stays byte-reproducible (rule 5).
    """
    import numpy as np

    rng = np.random.default_rng(int(seed))
    mode_of = _unit_event_mode(readings)
    unit_class = units.set_index("unit_id")["vehicle_class_id"].to_dict()

    def _spread_pick(candidates: list[str], k: int) -> list[str]:
        # Round-robin across classes (each class's units shuffled by the seed) so the
        # quota is filled with as even a class spread as the pool allows.
        by_class: dict[object, list[str]] = {}
        for uid in sorted(candidates):
            by_class.setdefault(unit_class.get(uid), []).append(uid)
        for lst in by_class.values():
            rng.shuffle(lst)
        picked: list[str] = []
        buckets = [by_class[c] for c in sorted(by_class, key=lambda c: str(c))]
        while len(picked) < k and any(buckets):
            for b in buckets:
                if b:
                    picked.append(b.pop())
                    if len(picked) >= k:
                        break
        return picked

    keep: list[str] = []
    for mode in FAILURE_MODES:
        cands = [u for u, m in mode_of.items() if m == mode]
        picked = _spread_pick(cands, UNITS_PER_MODE)
        if len(picked) < UNITS_PER_MODE:
            raise SystemExit(
                f"fixture: only {len(picked)} units fail with mode '{mode}' "
                f"(need {UNITS_PER_MODE}); widen the fleet or lower UNITS_PER_MODE."
            )
        keep.extend(picked)
    healthy = [u for u, m in mode_of.items() if m == ""]
    keep.extend(_spread_pick(healthy, HEALTHY_UNITS))
    return sorted(keep)


def build() -> Path:
    # Load and use the SAME canonical config the full pipeline uses — the fixture keeps
    # the full 90-day window (unreduced); only units + time-stride are reduced below.
    cfg = load_config(DATASET_CONFIG)
    ds = simulate(cfg)
    df = ds.readings

    present = [c for c in KEEP_COLUMNS if c in df.columns]
    missing = [c for c in KEEP_COLUMNS if c not in df.columns]
    if missing:
        print(f"note: columns not in this generator version, skipped: {missing}")
    slim = df[present].copy()

    # 1) keep a mode-stratified subset of units so ALL failure modes are present
    #    (reuse the canonical seed for a reproducible pick)
    keep_units = _mode_stratified_units(df, ds.units, cfg.seed)
    slim = slim[slim["unit_id"].isin(keep_units)].copy()

    # 2) time-downsample by striding each unit's series (timestamp_h is monotonic per
    #    unit), so the committed artifact stays tiny. Compute the per-unit positional
    #    index without groupby.cumcount (a numpy/pandas broadcast quirk on some builds):
    #    after sorting by unit, each unit's rows are contiguous, so per-group arange
    #    concatenation lines up with groupby.size()'s key-sorted order.
    import numpy as np

    slim = slim.sort_values(["unit_id", "timestamp_h"]).reset_index(drop=True)
    grp_sizes = slim.groupby("unit_id", observed=True).size().to_numpy()
    pos_within_unit = np.concatenate([np.arange(s) for s in grp_sizes])
    slim = slim[pos_within_unit % TIME_STRIDE == 0].reset_index(drop=True)

    # Shrink the on-disk fixture: float32 for the signals, categoricals for the
    # low-cardinality label strings. This is a *smoke fixture only* — never the
    # training set (the real pipeline regenerates the full dataset from the
    # committed config + seed; see ADR-001 and configs/dataset.json).
    float_cols = slim.select_dtypes("float64").columns
    slim[float_cols] = slim[float_cols].astype("float32")
    for col in ("failure_mode", "anomaly_type"):
        if col in slim:
            slim[col] = slim[col].astype("category")

    out = Path(__file__).resolve().parents[1] / "data" / "sample_readings.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    slim.to_parquet(out, index=False, compression="zstd")

    # Fail loud if any failure mode is missing — that is the exact defect this rewrite
    # fixes (a single-mode fixture → a demo model that only knows one failure).
    mode_counts = slim.loc[slim["failure_within_h"] == 1, "failure_mode"].astype(str).value_counts()
    absent = [m for m in FAILURE_MODES if mode_counts.get(m, 0) == 0]
    if absent:
        raise SystemExit(f"fixture is missing failure mode(s): {absent} — coverage broke.")

    pos = int(slim["failure_within_h"].sum())
    size_kb = out.stat().st_size / 1024
    print(f"wrote {out}  ({size_kb:,.0f} KB)")
    print(f"  {len(slim):,} rows × {len(present)} cols · {slim['unit_id'].nunique()} units")
    print(f"  failure rows: {pos:,} ({pos / max(len(slim), 1):.1%})")
    print(f"  by mode: {dict(mode_counts)}")
    return out


if __name__ == "__main__":
    build()
