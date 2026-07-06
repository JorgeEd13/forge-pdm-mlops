"""F7 demo-UI tests — the /demo page and /demo/predict endpoint (ADR-015).

Offline, like test_serve.py: a tiny model is trained on the committed fixture, registered,
and promoted to ``production`` in a tmp SQLite MLflow backend, and the demo endpoints are
driven through a ``TestClient``. These assert (1) the demo prediction round-trips exactly
like ``/predict``, (2) when a prediction log is injected the served rows are **persisted**
and (3) surface on the ``/demo`` page, and (4) with **no** log the demo still works and
says so — the graceful-degrade contract. Needs both the ``[serve]`` and ``[cloud]`` extras
for the persistence tests; the no-persistence tests need only ``[serve]``.
"""

from __future__ import annotations

import pandas as pd
import pytest
from mlflow.tracking import MlflowClient

pytest.importorskip("fastapi", reason="needs the `[serve]` extra (F4/ADR-009)")
pytest.importorskip("httpx", reason="needs the `[serve]` extra (F4/ADR-009)")

from fastapi.testclient import TestClient  # noqa: E402

from pdm_mlops import config, features, registry, serve, store_pg, train  # noqa: E402

NAME = config.REGISTERED_MODEL_NAME

_HAS_CLOUD = True
try:  # the [cloud] extra — the persistence half of the demo
    import sqlalchemy  # noqa: F401
except ImportError:  # pragma: no cover - exercised where [cloud] is absent
    _HAS_CLOUD = False

needs_cloud = pytest.mark.skipif(not _HAS_CLOUD, reason="needs the `[cloud]` extra (F7/ADR-015)")


@pytest.fixture
def tmp_tracking(tmp_path):
    return config.sqlite_tracking_uri(tmp_path / "mlflow.db")


@pytest.fixture
def fixture_readings() -> pd.DataFrame:
    return pd.read_parquet(config.SAMPLE_READINGS)


def _train_and_promote(tmp_tracking: str, readings: pd.DataFrame) -> str:
    """Train on the fixture (class-rich seed), register + promote — return the version."""
    summary = train.train(seed=0, tracking_uri=tmp_tracking, readings=readings, register=True)
    client = MlflowClient(tracking_uri=tmp_tracking, registry_uri=tmp_tracking)
    assert registry.promote(client, NAME, summary.registered_version).promoted
    return str(summary.registered_version)


def _rows(readings: pd.DataFrame, n: int = 2) -> list[dict]:
    frame = readings.loc[:, list(features.FEATURE_COLUMNS)].head(n)
    return [
        {k: (None if pd.isna(v) else float(v)) for k, v in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _app(tmp_tracking: str, log: store_pg.PredictionLog | None):
    store = serve.ModelStore(tracking_uri=tmp_tracking)
    return TestClient(serve.create_app(store=store, prediction_log=log))


# --- the demo prediction round-trips (no persistence) -------------------------


def test_demo_predict_round_trips_without_a_log(tmp_tracking, fixture_readings) -> None:
    _train_and_promote(tmp_tracking, fixture_readings)
    client = _app(tmp_tracking, log=None)

    resp = client.post("/demo/predict", json={"readings": _rows(fixture_readings, 2)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["n_rows"] == 2
    assert all(0.0 <= p <= 1.0 for p in body["failure_probability"])
    # No DATABASE_URL → not persisted, and the response says so honestly.
    assert body["persisted"] is False


def test_demo_predict_503_without_a_promoted_model(tmp_tracking, fixture_readings) -> None:
    # Same 503 contract as /predict when nothing is promoted (not a 500).
    client = _app(tmp_tracking, log=None)
    resp = client.post("/demo/predict", json={"readings": _rows(fixture_readings, 1)})
    assert resp.status_code == 503


def test_demo_page_renders_with_the_honesty_banner(tmp_tracking, fixture_readings) -> None:
    _train_and_promote(tmp_tracking, fixture_readings)
    client = _app(tmp_tracking, log=None)

    resp = client.get("/demo")
    assert resp.status_code == 200
    page = resp.text
    # The demo=fixture honesty boundary is on the page (matches /model-info + README).
    assert "DEMO model" in page
    assert "not" in page.lower() and "reported result" in page.lower()
    # Every feature signal has a form field.
    assert all(sig in page for sig in features.FEATURE_COLUMNS)
    # With no DB the page says logging is off (no managed-DB panel).
    assert "off" in page.lower()


# --- F9: friendly inputs + light/dark theme + EN/PT-BR i18n (ADR-018) ----------
# The GET /demo page renders without a promoted model (it only reads the log), so
# these assert the self-contained shell directly — no train/promote needed.


def _demo_page(log=None) -> str:
    return serve._render_demo_page(log or [], persistence=log is not None)


def test_demo_page_is_theme_aware_and_self_contained() -> None:
    page = _demo_page()
    # Light/dark theming via prefers-color-scheme AND a persisted data-theme override
    # that must win in both directions (the manual toggle).
    assert "prefers-color-scheme: dark" in page
    assert '[data-theme="dark"]' in page and '[data-theme="light"]' in page
    # Clean-room / offline: no external asset (no CDN link, script src, or @import).
    assert "http://" not in page and "https://" not in page
    assert "<script src" not in page and "@import" not in page


def test_demo_page_ships_both_locales_and_holds_the_honesty_line_in_each() -> None:
    page = _demo_page()
    # Both locales are injected into the page (client-side toggle, no reload).
    assert '"en"' in page and '"pt-BR"' in page
    # The demo=fixture / not-a-reported-result honesty boundary survives in BOTH
    # languages (ADR-018: i18n localizes the UI, never softens the honesty framing).
    assert "not" in page and "reported result" in page  # EN banner
    assert "não" in page and "resultado reportado" in page  # PT-BR banner
    # The ≈0.82 framing is intact in each language.
    assert page.count("0.82") >= 2


def test_demo_page_has_friendly_presets_units_and_tooltips() -> None:
    page = _demo_page()
    # One-click presets (the biggest UX win): a whole plausible row per click.
    assert all(k in page for k in ("healthy", "bearing", "overheat"))
    assert 'data-preset="bearing"' in page
    # Every signal carries a unit + a bounded range + a tooltip so a domain-naive
    # tester sets sane values instead of guessing free-text.
    for name, meta in serve._SIGNAL_META.items():
        assert name in page and str(meta["unit"]) in page
    # A localized honesty note that i18n localizes the UI, not the prediction.
    assert "i18nNote" in page


def test_presets_cover_every_feature_signal() -> None:
    # Each preset must fill the full feature vector — a partial preset would leave a
    # field at its stale value and mislead the tester about what it scored.
    for name, row in serve._PRESETS.items():
        missing = [s for s in features.FEATURE_COLUMNS if s not in row]
        assert not missing, f"preset {name} missing {missing}"


def test_signal_ranges_bracket_the_healthy_seed() -> None:
    # Every friendly [min, max] range must contain the seeded healthy value, or the
    # slider would clamp the default the moment the page loads.
    healthy = serve._PRESETS["healthy"]
    for name, meta in serve._SIGNAL_META.items():
        v = healthy[name]
        assert meta["min"] <= v <= meta["max"], f"{name}={v} outside [{meta['min']},{meta['max']}]"


# --- persistence to the managed DB (the F7 gate-relevant half) ----------------


@needs_cloud
def test_demo_predict_persists_to_the_log(tmp_tracking, fixture_readings, tmp_path) -> None:
    version = _train_and_promote(tmp_tracking, fixture_readings)
    log = store_pg.open_log(f"sqlite:///{(tmp_path / 'demo.db').as_posix()}")
    client = _app(tmp_tracking, log=log)

    resp = client.post("/demo/predict", json={"readings": _rows(fixture_readings, 3)})
    assert resp.status_code == 200
    assert resp.json()["persisted"] is True

    # The three served rows were logged (newest-first), tagged with the served version.
    recent = log.recent(limit=10)
    assert len(recent) == 3
    assert all(r.model_version == version for r in recent)
    log.dispose()


@needs_cloud
def test_demo_page_shows_recent_predictions(tmp_tracking, fixture_readings, tmp_path) -> None:
    _train_and_promote(tmp_tracking, fixture_readings)
    log = store_pg.open_log(f"sqlite:///{(tmp_path / 'demo.db').as_posix()}")
    client = _app(tmp_tracking, log=log)

    client.post("/demo/predict", json={"readings": _rows(fixture_readings, 1)})
    page = client.get("/demo").text
    # With a DB configured the page advertises the managed Postgres panel and a row table.
    assert "Postgres" in page
    assert "<table" in page
    log.dispose()
