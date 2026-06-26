"""Single source of truth for paths, MLflow wiring, seeds and thresholds.

Kept deliberately small and dependency-free so every other module (and the tests)
imports its constants from one place. Values are plain module-level constants now;
they become a frozen config object if/when the surface grows (mirrors the
``ForgeConfig`` discipline in the companion generator).
"""

from __future__ import annotations

from pathlib import Path

# --- Repository layout ------------------------------------------------------

# ``src/pdm_mlops/config.py`` → repo root is two parents up from this file's dir.
REPO_ROOT: Path = Path(__file__).resolve().parents[2]
DATA_DIR: Path = REPO_ROOT / "data"
CONFIGS_DIR: Path = REPO_ROOT / "configs"

#: The canonical training-dataset spec — the **single cross-machine source of
#: truth** (ADR-001). The data layer regenerates the FULL dataset from this file +
#: the pinned generator version, so every machine produces byte-identical data.
DATASET_CONFIG: Path = CONFIGS_DIR / "dataset.json"

#: A tiny, committed, *reduced* slice of the generator's ``readings`` table.
#: **Purpose: offline/CI smoke only — NOT a training set.** Models are always
#: trained on the full dataset regenerated from ``DATASET_CONFIG`` (ADR-001); this
#: fixture exists so ``clone && pytest`` and CI run without the generator or a
#: network. ``data.py`` falls back to it only when the generator is unavailable.
SAMPLE_READINGS: Path = DATA_DIR / "sample_readings.parquet"

# --- Determinism ------------------------------------------------------------

#: One default seed threaded through data generation, splitting and training so a
#: run is byte-reproducible — mirrors the generator's hard determinism invariant.
DEFAULT_SEED: int = 42

# --- MLflow -----------------------------------------------------------------

#: Local backend, no server / no paid service (ADR-002). MLflow 3 put the bare
#: ``./mlruns`` *file store* into maintenance mode, so tracking + the model registry
#: use a local **SQLite** database (still just a file on disk, no daemon), while model
#: artifacts are written to a plain ``mlartifacts/`` directory. Both are git-ignored
#: and fully reproducible from a seed.
MLRUNS_DIR: Path = REPO_ROOT / "mlruns"
MLFLOW_DB: Path = MLRUNS_DIR / "mlflow.db"
MLFLOW_ARTIFACTS_DIR: Path = REPO_ROOT / "mlartifacts"
EXPERIMENT_NAME: str = "forge-pdm-failure"
REGISTERED_MODEL_NAME: str = "forge-pdm-failure-classifier"


def sqlite_tracking_uri(db_path: Path) -> str:
    """A SQLite MLflow tracking URI for ``db_path`` (server-free local backend)."""
    return f"sqlite:///{db_path.as_posix()}"

# --- Modelling --------------------------------------------------------------

#: Prediction target column in the generator's ``readings`` table (D5): does the
#: unit experience a failure within the label horizon.
TARGET: str = "failure_within_h"

#: Eval metric that gates model selection (F2) and registry promotion (F3).
PRIMARY_METRIC: str = "roc_auc"

# --- Drift (F5) -------------------------------------------------------------

#: The generator season used as the drift stimulus in the marquee loop. Baseline
#: training data uses no season; the monitor compares against this shifted one.
DRIFT_SEASON: str = "heatwave"
