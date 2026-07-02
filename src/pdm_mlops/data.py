"""Data layer — produce the ``readings`` table the modelling layer consumes.

Two sources, one honest contract (ADR-001):

* **Full regeneration (the real path).** When the pinned ``can-telemetry-forge``
  generator is installed, regenerate the *full* dataset from
  :data:`config.DATASET_CONFIG` — the single cross-machine source of truth. Every
  machine produces byte-identical data from the same config + pinned generator.
* **Offline fixture fallback (smoke only).** When the generator is **absent**, fall
  back **loudly** to the committed ``data/sample_readings.parquet`` smoke fixture so
  ``clone && pytest`` and CI run with no network and no generator. The fixture is a
  reduced slice — never a training set; callers asking for the full dataset are
  warned that they got the fixture instead.

The ``season`` argument lets the F5 drift loop request a distribution-shifted dataset
(e.g. ``"heatwave"``) from the *same* config; it only affects regeneration and is
meaningless for the fixture (which the fallback flags).
"""

from __future__ import annotations

import warnings
from dataclasses import replace
from pathlib import Path

import pandas as pd

from . import config


class GeneratorUnavailable(RuntimeError):
    """Raised when the full dataset is required but the generator is not installed."""


def generator_available() -> bool:
    """True iff the pinned ``can-telemetry-forge`` generator can be imported."""
    try:
        import can_telemetry_forge  # noqa: F401
    except Exception:
        return False
    return True


def regenerate_full(
    *,
    season: str | None = None,
    config_path: Path | None = None,
) -> pd.DataFrame:
    """Regenerate the **full** ``readings`` dataset from the canonical config.

    Requires the generator (raises :class:`GeneratorUnavailable` otherwise — the
    caller decides whether to fall back). ``season`` overrides the config's season
    in place (the F5 drift stimulus); everything else stays canonical so the run
    is reproducible from the committed recipe + pinned generator.
    """
    if not generator_available():
        raise GeneratorUnavailable(
            "can-telemetry-forge is not installed; install the '[generate]' extra "
            "to regenerate the full dataset (see ADR-001)."
        )
    from can_telemetry_forge.config import load_config, resolve_season
    from can_telemetry_forge.sim.simulate import simulate

    cfg = load_config(str(config_path or config.DATASET_CONFIG))
    if season is not None:
        # The generator's config carries a `Season` object (ambient delta / wear /
        # hazard multipliers), not the bare preset name; resolve the string to that
        # preset the same way the generator's own CLI does (`--season`).
        cfg = replace(cfg, season=resolve_season(season))
    dataset = simulate(cfg)
    return dataset.readings


def load_fixture() -> pd.DataFrame:
    """Load the committed offline smoke fixture (reduced slice — not a training set)."""
    if not config.SAMPLE_READINGS.exists():
        raise FileNotFoundError(
            f"smoke fixture missing at {config.SAMPLE_READINGS}; "
            "run scripts/build_sample.py to regenerate it (needs the generator)."
        )
    return pd.read_parquet(config.SAMPLE_READINGS)


def load_readings(
    *,
    season: str | None = None,
    allow_fixture_fallback: bool = True,
    config_path: Path | None = None,
) -> pd.DataFrame:
    """Return the ``readings`` table, preferring full regeneration.

    * Generator present → the **full** regenerated dataset (the real path).
    * Generator absent and ``allow_fixture_fallback`` → the committed fixture, with a
      **loud** warning that this is the reduced smoke slice (never report metrics off
      it; ADR-001). If a ``season`` was requested, the warning also flags that the
      fixture ignores it.
    * Generator absent and not allowed to fall back → :class:`GeneratorUnavailable`.

    Determinism: the full path is byte-reproducible from the pinned config + seed.
    """
    if generator_available():
        return regenerate_full(season=season, config_path=config_path)

    if not allow_fixture_fallback:
        raise GeneratorUnavailable(
            "the full dataset was requested but can-telemetry-forge is not installed, "
            "and fixture fallback is disabled."
        )

    season_note = (
        f" The requested season={season!r} is IGNORED by the fixture." if season else ""
    )
    warnings.warn(
        "can-telemetry-forge is not installed — falling back to the committed SMOKE "
        "FIXTURE (data/sample_readings.parquet). This is a reduced slice for offline "
        "plumbing only; do NOT treat its metrics as reported results (ADR-001)."
        + season_note,
        stacklevel=2,
    )
    return load_fixture()
