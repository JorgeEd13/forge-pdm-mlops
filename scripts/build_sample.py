"""Regenerate the committed offline **smoke fixture** (``data/sample_readings.parquet``).

This bakes a tiny, reduced slice of the generator's ``readings`` table into the
repo so ``clone && pytest`` and CI run fully offline (ADR-001). It is **not** a
training set — the real pipeline always regenerates the *full* dataset from
``configs/dataset.json`` (the cross-machine source of truth). To keep the fixture
honestly representative, it is built by *reducing the very same canonical config*
(fewer units, a coarser stride), never an independently-parameterized run.

Run from the repo root after ``pip install -e '.[generate]'``::

    python scripts/build_sample.py

Deterministic: same generator version + canonical config → byte-identical fixture.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from can_telemetry_forge.config import load_config
from can_telemetry_forge.sim.simulate import simulate

# The canonical training-dataset spec the full pipeline uses — the fixture is a
# strict reduction of THIS, so it can never silently diverge from the real data.
DATASET_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "dataset.json"

# The fixture is the canonical config REDUCED on three axes (documented here so the
# reduction is explicit and reproducible), preserving the fleet's structure (class
# mix, era-NULL missingness, multi-mode labels) while staying well under ~1 MB:
SAMPLE_DAYS = 14            # shorter window than the canonical 90d (fixture only)
TIME_STRIDE = 12           # keep every 12th 5-min sample → ~hourly on disk
SAMPLE_UNITS = 24          # stratified across vehicle classes, keeps class/era spread

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
    "failure_within_h",
    "failure_mode",
    "anomaly_type",
    "is_outlier",
]


def _stratified_unit_sample(units, n: int, seed: int) -> list[str]:
    """Pick ~``n`` unit_ids spread across vehicle classes (and thus eras), so the
    tiny slice keeps the class mix and the era-NULL missingness structure."""
    import numpy as np

    rng = np.random.default_rng(int(seed))
    classes = units["vehicle_class_id"].unique()
    per_class = max(1, n // len(classes))
    picked: list[str] = []
    for cls in classes:
        ids = units.loc[units["vehicle_class_id"] == cls, "unit_id"].to_numpy()
        take = min(per_class, len(ids))
        picked.extend(rng.choice(ids, size=take, replace=False).tolist())
    return sorted(picked)


def build() -> Path:
    # Load the SAME canonical config the full pipeline uses, then reduce only the
    # window (days) for the fixture — seed/resolution/horizon stay canonical.
    canonical = load_config(DATASET_CONFIG)
    cfg = replace(canonical, days=SAMPLE_DAYS)
    ds = simulate(cfg)
    df = ds.readings

    present = [c for c in KEEP_COLUMNS if c in df.columns]
    missing = [c for c in KEEP_COLUMNS if c not in df.columns]
    if missing:
        print(f"note: columns not in this generator version, skipped: {missing}")
    slim = df[present].copy()

    # 1) keep a stratified subset of units (reuse the canonical seed)
    keep_units = _stratified_unit_sample(ds.units, SAMPLE_UNITS, cfg.seed)
    slim = slim[slim["unit_id"].isin(keep_units)].copy()

    # 2) time-downsample to ~hourly by striding each unit's series (timestamp_h is
    #    monotonic per unit), so the committed artifact stays tiny.
    slim = slim.sort_values(["unit_id", "timestamp_h"])
    keep_row = slim.groupby("unit_id").cumcount() % TIME_STRIDE == 0
    slim = slim[keep_row].reset_index(drop=True)

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
    pos = int(slim["failure_within_h"].sum())
    print(f"wrote {out}")
    print(f"  {len(slim):,} rows × {len(present)} cols · {slim['unit_id'].nunique()} units")
    print(f"  failure rows: {pos:,} ({pos / max(len(slim), 1):.1%})")
    return out


if __name__ == "__main__":
    build()
