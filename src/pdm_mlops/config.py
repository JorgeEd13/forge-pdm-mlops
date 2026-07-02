"""Single source of truth for paths, MLflow wiring, seeds and thresholds.

Kept deliberately small and dependency-free so every other module (and the tests)
imports its constants from one place. Values are plain module-level constants now;
they become a frozen config object if/when the surface grows (mirrors the
``ForgeConfig`` discipline in the companion generator).
"""

from __future__ import annotations

import os
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


def default_tracking_uri() -> str:
    """The MLflow tracking/registry URI the pipeline uses when none is injected.

    Honours the standard ``MLFLOW_TRACKING_URI`` env var if set — so the serving
    container (F4) can point at a mounted registry volume without a code change — and
    otherwise falls back to the local ``mlruns/mlflow.db`` SQLite backend, creating its
    directory. Tests always inject a tmp URI and never touch this.
    """
    override = os.environ.get("MLFLOW_TRACKING_URI")
    if override:
        return override
    MLRUNS_DIR.mkdir(parents=True, exist_ok=True)
    return sqlite_tracking_uri(MLFLOW_DB)

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

#: Retrain-trigger policy (ADR-013): drift is declared when at least this **share of
#: the model's input features** drift (per Evidently's per-column tests). A share, not
#: "any one feature", so a single column tripping on noise does not fire a retrain — the
#: loop should react to a distribution shift, which the ``season`` stimulus produces
#: across several correlated signals at once. Set to **one third** so the trigger fires
#: when a *correlated cluster* moves together (the real ``heatwave`` footprint is ~3-4 of
#: the 9 signals: ``coolant_temp_c`` via ambient, plus the wear-coupled ``oil_pressure_kpa``
#: / ``vibration_mms``) while still rejecting one or two noisy columns. Deliberately a
#: *fraction of the surface*, not a fixed count, but chosen against the physics rather than
#: the feature count — the earlier 0.5 was an artifact of the pre-``vibration_mms`` 8-signal
#: surface (4/8) that silently became "≥5 of 9" when the 9th feature was added (ADR-013).
DRIFT_SHARE_THRESHOLD: float = 1.0 / 3.0
