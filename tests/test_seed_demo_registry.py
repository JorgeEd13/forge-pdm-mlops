"""F6 tests — the demo-registry bake for the hosted deploy (`scripts/seed_demo_registry.py`).

All offline: the script trains on the committed smoke fixture and promotes into a
self-contained SQLite MLflow store under a tmp dir. These assert the F6 DoD building
blocks — that the baked store yields a **promoted `production` version a fresh serving
process can load** (`model_loaded=true`), that the version is **tagged a fixture demo**
(the honest-status boundary, ADR-014), and that the store is **self-contained** (artifacts
colocated with the DB so the image can carry it).

The script pulls in FastAPI via the serve round-trip, so this module skips cleanly when
the `[serve]` extra is absent — same pattern as test_serve.py.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="needs the `[serve]` extra (F4/ADR-009)")
pytest.importorskip("httpx", reason="needs the `[serve]` extra (F4/ADR-009)")

from fastapi.testclient import TestClient  # noqa: E402

from pdm_mlops import config, features, registry, serve  # noqa: E402


def _load_seed_module():
    """Import the script by path (it lives under scripts/, not the installed package)."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "seed_demo_registry.py"
    spec = importlib.util.spec_from_file_location("seed_demo_registry", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["seed_demo_registry"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seed_mod():
    return _load_seed_module()


def test_bake_promotes_a_demo_version(seed_mod, tmp_path):
    """The bake registers + promotes a version and tags it a fixture demo."""
    store = tmp_path / "mlflow"
    version = seed_mod.seed_registry(store)

    uri = config.sqlite_tracking_uri(store / "mlflow.db")
    client = registry._client(uri)
    # It is the live production version.
    assert registry.production_version(client, config.REGISTERED_MODEL_NAME) == version
    # And it is unmistakably flagged as a fixture demo (the ADR-014 honesty boundary).
    mv = client.get_model_version(config.REGISTERED_MODEL_NAME, version)
    assert mv.tags.get("demo") == "fixture"


def test_baked_store_serves_model_loaded_true(seed_mod, tmp_path):
    """The F6 DoD in miniature: a FRESH serving process over the baked store predicts.

    The store is loaded by a `ModelStore` bound to the store's URI (a distinct object
    from the one the bake used), mirroring the deployed container reading a baked image.
    """
    store = tmp_path / "mlflow"
    seed_mod.seed_registry(store)

    uri = config.sqlite_tracking_uri(store / "mlflow.db")
    app = serve.create_app(serve.ModelStore(tracking_uri=uri))
    client = TestClient(app)

    health = client.get("/health").json()
    assert health["model_loaded"] is True and health["model_version"] is not None

    row = {col: 1.0 for col in features.FEATURE_COLUMNS}
    resp = client.post("/predict", json={"readings": [row]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["n_rows"] == 1
    assert 0.0 <= body["failure_probability"][0] <= 1.0


def test_store_is_self_contained(seed_mod, tmp_path):
    """DB + artifacts are colocated under the store dir, so the image can carry it.

    MLflow otherwise scatters artifacts to a CWD-relative ``mlruns/``; the script pins an
    explicit experiment ``artifact_location`` inside the store. Assert the artifacts landed
    there (a model artifact exists under the store tree), not outside it.
    """
    store = tmp_path / "mlflow"
    seed_mod.seed_registry(store)

    assert (store / "mlflow.db").is_file()
    artifacts = list((store / "artifacts").rglob("MLmodel"))
    assert artifacts, "no model artifact colocated under the store dir"


def test_bake_is_deterministic(seed_mod, tmp_path):
    """Same fixed seed → the same demo model probabilities (byte-reproducible build)."""
    def _probs(dir_name: str) -> list[float]:
        store = tmp_path / dir_name
        seed_mod.seed_registry(store)
        uri = config.sqlite_tracking_uri(store / "mlflow.db")
        client = TestClient(serve.create_app(serve.ModelStore(tracking_uri=uri)))
        rows = [{col: float(i) for col in features.FEATURE_COLUMNS} for i in range(1, 4)]
        return client.post("/predict", json={"readings": rows}).json()["failure_probability"]

    assert _probs("a") == pytest.approx(_probs("b"))
