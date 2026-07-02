"""F7 prediction-log tests — the managed-DB persistence layer (ADR-015).

All offline: the SAME ``store_pg`` code that runs against Cloud SQL for Postgres in
production runs here against a per-test **tmp SQLite** file, so the append/read contract,
the no-PII key restriction, era-NULL preservation, and — crucially — the **graceful
degrade** (no ``DATABASE_URL`` ⇒ no log, never a crash) are all exercised without a
server. SQLAlchemy is the ``[cloud]`` extra, so the module skips cleanly when it is
absent (core CI installs only ``[dev]``), mirroring the ``[serve]``/``[ops]`` skips.
"""

from __future__ import annotations

import pytest

# SQLAlchemy lives in the optional `[cloud]` extra (F7/ADR-015). The whole log layer needs
# it, so skip the entire module when it is absent — the same pattern as test_serve.py's
# `[serve]` skip. store_pg imports it lazily, so importing pdm_mlops never needs it.
pytest.importorskip("sqlalchemy", reason="needs the `[cloud]` extra (F7/ADR-015)")

from pdm_mlops import features, store_pg  # noqa: E402


@pytest.fixture
def log(tmp_path):
    """A prediction log bound to a throwaway SQLite file (stands in for Cloud SQL)."""
    lg = store_pg.open_log(f"sqlite:///{(tmp_path / 'demo.db').as_posix()}")
    assert lg is not None
    yield lg
    lg.dispose()


# --- graceful degrade: the whole point of the design --------------------------


def test_open_log_returns_none_without_a_url(monkeypatch) -> None:
    # No DATABASE_URL (local / HF Space / CI) → no log, no error. The demo then runs
    # without persistence; nothing about serving depends on the managed DB.
    monkeypatch.delenv(store_pg.DATABASE_URL_ENV, raising=False)
    assert store_pg.open_log() is None


def test_open_log_reads_the_env_var(monkeypatch, tmp_path) -> None:
    url = f"sqlite:///{(tmp_path / 'env.db').as_posix()}"
    monkeypatch.setenv(store_pg.DATABASE_URL_ENV, url)
    lg = store_pg.open_log()
    assert lg is not None
    lg.dispose()


def test_open_log_degrades_on_a_bad_url() -> None:
    # A malformed / unreachable URL must degrade to None, not crash app startup.
    assert store_pg.open_log("not-a-real-scheme://nowhere") is None


# --- the append/read round-trip -----------------------------------------------


def test_log_then_recent_round_trips(log) -> None:
    log.log(model_version="1", failure_probability=0.73, readings={"coolant_temp_c": 95.0})
    log.log(model_version="1", failure_probability=0.11, readings={"coolant_temp_c": 70.0})

    recent = log.recent(limit=10)
    assert len(recent) == 2
    # Newest first.
    assert recent[0].failure_probability == pytest.approx(0.11)
    assert recent[1].failure_probability == pytest.approx(0.73)
    assert recent[0].model_version == "1"
    # Timestamp is tz-aware UTC (SQLite drops the tz on the round-trip; store_pg restores it).
    assert recent[0].created_at.tzinfo is not None


def test_recent_respects_the_limit(log) -> None:
    for i in range(5):
        log.log(model_version="1", failure_probability=i / 10, readings={"coolant_temp_c": 80.0})
    assert len(log.recent(limit=3)) == 3


# --- no PII / no schema surprises ---------------------------------------------


def test_readings_are_restricted_to_known_signals(log) -> None:
    # A crafted request key must NOT be stored — only the model's feature signals are kept,
    # so the row can never carry arbitrary (potentially identifying) data.
    log.log(
        model_version="1",
        failure_probability=0.5,
        readings={"coolant_temp_c": 90.0, "user_email": "leak@example.com", "note": "x"},
    )
    stored = log.recent(limit=1)[0].readings
    assert "user_email" not in stored
    assert "note" not in stored
    assert set(stored.keys()) == set(features.FEATURE_COLUMNS)
    assert stored["coolant_temp_c"] == pytest.approx(90.0)


def test_missing_signals_are_stored_as_era_null(log) -> None:
    # A signal the request omitted is stored as None (era-NULL), not dropped — every known
    # column is present so the schema is stable.
    log.log(model_version="1", failure_probability=0.4, readings={"coolant_temp_c": 88.0})
    stored = log.recent(limit=1)[0].readings
    assert stored["vibration_mms"] is None
    assert stored["coolant_temp_c"] == pytest.approx(88.0)


# --- best-effort logging never breaks the request path ------------------------


def test_log_swallows_a_backend_error(log, monkeypatch) -> None:
    # A transient DB error (the managed instance briefly unreachable) must NEVER raise into
    # the request path — the model already answered; a missing log row is the only cost.
    # Force the engine to fail on use, then assert both log() and recent() degrade quietly.
    def _boom(*a, **k):
        raise RuntimeError("backend down")

    monkeypatch.setattr(log._engine, "begin", _boom)
    monkeypatch.setattr(log._engine, "connect", _boom)

    # No exception escapes either call.
    log.log(model_version="1", failure_probability=0.9, readings={"coolant_temp_c": 100.0})
    assert log.recent(limit=5) == []
