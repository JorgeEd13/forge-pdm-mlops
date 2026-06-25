"""forge-pdm-mlops — an MLOps pipeline over synthetic predictive-maintenance telemetry.

The data source is the companion ``can-telemetry-forge`` generator: this package
trains on its J1939-grounded ``readings`` table, tracks experiments and registers
models with MLflow, serves the promoted model with FastAPI, and closes a
**drift → auto-retrain** loop (the generator's ``season`` knob shifts the
distribution; the pipeline detects the drift and retrains).

The package is built phase by phase (see ``docs/ROADMAP.md``); this module stays
light so importing it is cheap and side-effect free.
"""

from __future__ import annotations

from importlib import metadata

try:
    __version__ = metadata.version("forge-pdm-mlops")
except metadata.PackageNotFoundError:  # pragma: no cover - source-tree fallback
    __version__ = "0.1.0"

__all__ = ["__version__"]
