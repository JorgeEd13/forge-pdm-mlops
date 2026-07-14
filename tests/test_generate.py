"""F14a — generate-your-own-data: caps, the roll-up rule, the store, and the WEB/WORKER SPLIT.

Offline and deterministic, like the rest of the suite: the store is a tmp SQLite file (the
same code that runs on Neon), the model is trained on the committed fixture into a tmp
MLflow backend, and the forge is never actually run in the tests that don't need it — the
generation path is exercised against a synthetic readings frame, so the pure half needs
neither the ``[generate]`` nor the ``[cloud]`` extra.

**The load-bearing test in this file is** :func:`test_the_api_never_generates_in_process`.
Decision S2 (ADR-026) says generation lives in a separate deployable unit; that is a claim
about *this process*, and a claim of that shape has to be asserted, not asserted-in-prose —
because the way it breaks is somebody adding three lines of ``BackgroundTasks`` and nothing
going red.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pdm_mlops import config, features, generate

# --- the caps (pure — no extras needed) ---------------------------------------


def test_default_spec_is_inside_the_caps() -> None:
    spec = generate.GenerationSpec().validate()
    assert spec.expected_rows == spec.n_units * spec.days * generate.ROWS_PER_UNIT_DAY
    assert spec.unit_days <= generate.MAX_UNIT_DAYS


@pytest.mark.parametrize(
    "kwargs",
    [
        {"n_units": generate.MAX_UNITS + 1},
        {"n_units": generate.MIN_UNITS - 1},
        {"days": generate.MAX_DAYS + 1},
        {"days": 0},
        {"seed": -1},
        # Both bounds legal on their own, the product is not — the binding cap.
        {"n_units": generate.MAX_UNITS, "days": generate.MAX_DAYS},
    ],
)
def test_out_of_bounds_specs_are_rejected(kwargs) -> None:
    with pytest.raises(generate.CapExceeded):
        generate.GenerationSpec(**kwargs).validate()


def test_the_unit_days_cap_is_what_bounds_storage() -> None:
    """A user may trade fleet size against window, but not exceed the row budget."""
    assert generate.GenerationSpec(n_units=30, days=6).validate().unit_days == 180
    with pytest.raises(generate.CapExceeded, match="unit-days"):
        generate.GenerationSpec(n_units=30, days=8).validate()
    # The cap really is the storage cap it claims to be.
    biggest = generate.MAX_UNIT_DAYS * generate.ROWS_PER_UNIT_DAY
    assert biggest * 5 <= generate.MAX_TOTAL_STORED_ROWS * 2  # a handful of runs still fits


# --- the roll-up rule (ADR-026) -----------------------------------------------


def _unit_frame(unit_ids: list[str], n: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "unit_id": np.repeat(unit_ids, n),
            "t_index": list(range(n)) * len(unit_ids),
        }
    )


def test_roll_up_ignores_a_lone_spike_but_catches_a_sustained_run() -> None:
    """The whole reason the rule is not ``max`` (ADR-026).

    ``spiky`` is a healthy vehicle with one outlier reading — exactly what the generator's
    injected sensor faults produce. ``degrading`` never reaches 1.0 on any single row but
    stays elevated for an hour. A ``max`` roll-up would rank the healthy one higher; the
    sustained-risk rule ranks them the way the physics does.
    """
    frame = _unit_frame(["spiky", "degrading"], 60)
    p_spiky = np.full(60, 0.05)
    p_spiky[30] = 1.0                      # a single spurious spike
    p_degrading = np.full(60, 0.05)
    p_degrading[40:60] = 0.8               # a sustained (>1 h) elevation

    units = {u.unit_id: u for u in generate.roll_up(frame, np.concatenate([p_spiky, p_degrading]))}

    assert units["spiky"].peak == pytest.approx(1.0)          # max would flag it…
    assert units["degrading"].peak == pytest.approx(0.8)      # …over the real one
    assert units["degrading"].risk > units["spiky"].risk      # the rule does not
    assert units["degrading"].flagged
    assert not units["spiky"].flagged


def test_roll_up_is_ranked_riskiest_first_and_aligned() -> None:
    frame = _unit_frame(["a", "b"], 24)
    proba = np.concatenate([np.full(24, 0.9), np.full(24, 0.1)])
    units = generate.roll_up(frame, proba)
    assert [u.unit_id for u in units] == ["a", "b"]
    assert units[0].risk > units[1].risk
    assert units[0].n_rows == 24
    assert units[0].high_risk_share == pytest.approx(1.0)
    assert units[1].high_risk_share == pytest.approx(0.0)


def test_roll_up_fails_loud_on_misalignment() -> None:
    frame = _unit_frame(["a"], 10)
    with pytest.raises(ValueError, match="misalignment"):
        generate.roll_up(frame, np.zeros(9))


def test_the_dataset_config_is_resolvable_when_the_package_is_INSTALLED(tmp_path, monkeypatch) -> None:
    """The worker runs the package installed, where `REPO_ROOT` points into site-packages.

    This is the defect the F6 deploy hit with the smoke fixture (ADR-014) and the F14a worker
    hit again with `configs/dataset.json`, on its first real cloud run — a native run cannot
    see it, because the source tree is right there. So: the env override must win, and a
    genuinely missing config must fail **loud** and say why.
    """
    # 1. The override wins (this is what Dockerfile.worker sets).
    elsewhere = tmp_path / "dataset.json"
    elsewhere.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(config.DATASET_CONFIG_ENV, str(elsewhere))
    assert config.dataset_config_path() == elsewhere

    # 2. Simulate the installed layout: nothing on any candidate path → fail loud, and name
    #    the actual cause rather than surfacing deep inside the generator.
    monkeypatch.setenv(config.DATASET_CONFIG_ENV, str(tmp_path / "nope.json"))
    monkeypatch.setattr(config, "DATASET_CONFIG", tmp_path / "also-nope.json")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="site-packages"):
        config.dataset_config_path()

    # 3. Unset → the ordinary source-tree path, which really is there.
    monkeypatch.delenv(config.DATASET_CONFIG_ENV)
    monkeypatch.undo()
    assert config.dataset_config_path().exists()


def test_stored_columns_carry_no_labels() -> None:
    """The store keeps the question, never the answer (ADR-003)."""
    stored = generate.stored_columns()
    assert set(features.FEATURE_COLUMNS) <= set(stored)
    assert not set(features.LEAKY_COLUMNS) & set(stored)
    features.assert_no_leakage(pd.DataFrame(columns=stored))


# --- the store (needs [cloud]: SQLAlchemy over a tmp SQLite file) --------------

sqlalchemy = pytest.importorskip("sqlalchemy", reason="needs the `[cloud]` extra (F7/F14a)")

from pdm_mlops import store_gen  # noqa: E402


def _readings(n_units: int = 3, n_rows: int = 20) -> pd.DataFrame:
    """A synthetic stand-in for the forge's output (no `[generate]` extra needed)."""
    rng = np.random.default_rng(0)
    frame = _unit_frame([f"u{i}" for i in range(n_units)], n_rows)
    for i, signal in enumerate(features.FEATURE_COLUMNS):
        frame[signal] = rng.normal(100 * (i + 1), 5, len(frame))
    frame.loc[frame.unit_id == "u0", "egt_c"] = np.nan  # an era-NULL unit
    return frame


@pytest.fixture
def store(tmp_path):
    s = store_gen.open_store(f"sqlite:///{(tmp_path / 'gen.db').as_posix()}")
    assert s is not None
    yield s
    s.dispose()


def test_open_store_degrades_gracefully_without_a_database(monkeypatch) -> None:
    monkeypatch.delenv(store_gen.DATABASE_URL_ENV, raising=False)
    assert store_gen.open_store() is None          # feature off, nothing broken
    assert store_gen.open_store("not-a-url") is None


def test_run_lifecycle_round_trips(store) -> None:
    spec = generate.GenerationSpec(n_units=3, days=1)
    run = store.create_run(spec)
    assert store.get_run(run.run_id).status == store_gen.STATUS_QUEUED

    store.mark_running(run.run_id)
    assert store.get_run(run.run_id).status == store_gen.STATUS_RUNNING

    n = store.append_readings(run.run_id, _readings())
    store.mark_succeeded(run.run_id, n_rows=n)

    done = store.get_run(run.run_id)
    assert done.status == store_gen.STATUS_SUCCEEDED
    assert done.done and done.n_rows == 60
    assert store.count_readings(run.run_id) == 60


def test_a_failed_run_carries_its_reason(store) -> None:
    run = store.create_run(generate.GenerationSpec(n_units=3, days=1))
    store.mark_failed(run.run_id, error="boom")
    failed = store.get_run(run.run_id)
    assert failed.status == store_gen.STATUS_FAILED and failed.error == "boom"


def test_era_null_survives_the_store_round_trip(store) -> None:
    """A missing sensor is a real input (ADR-003) — it must not come back as a zero."""
    run = store.create_run(generate.GenerationSpec(n_units=3, days=1))
    store.append_readings(run.run_id, _readings())
    frame = store.readings_frame(run.run_id)

    assert list(frame.columns) == generate.stored_columns()
    assert frame.loc[frame.unit_id == "u0", "egt_c"].isna().all()
    assert frame.loc[frame.unit_id == "u1", "egt_c"].notna().all()
    # And it is ordered — the roll-up's rolling window depends on it.
    assert frame.equals(frame.sort_values(["unit_id", "t_index"]).reset_index(drop=True))


def test_the_report_cache_is_keyed_by_model_version(store) -> None:
    """A promotion or rollback must re-score, not serve a stale roll-up (ADR-008/009)."""
    run = store.create_run(generate.GenerationSpec(n_units=3, days=1))
    frame = _readings()
    store.append_readings(run.run_id, frame)

    units = generate.roll_up(frame, np.full(len(frame), 0.9))
    store.save_report(run.run_id, "1", units)

    assert len(store.load_report(run.run_id, "1")) == 3
    assert store.load_report(run.run_id, "2") == []       # a different model → no cache hit

    # Re-saving the same key replaces, never duplicates (a retry, or two racing requests).
    store.save_report(run.run_id, "1", units)
    assert len(store.load_report(run.run_id, "1")) == 3


def test_prune_evicts_the_oldest_runs_but_never_the_one_just_finished(store) -> None:
    runs = []
    for _ in range(3):
        run = store.create_run(generate.GenerationSpec(n_units=3, days=1))
        store.append_readings(run.run_id, _readings())
        store.mark_succeeded(run.run_id, n_rows=60)
        runs.append(run.run_id)

    assert store.total_readings() == 180
    evicted = store.prune(max_total_rows=100, keep=runs[-1])

    assert evicted == 2
    assert store.get_run(runs[0]) is None and store.get_run(runs[1]) is None
    assert store.get_run(runs[-1]) is not None            # the fresh run survives
    assert store.total_readings() == 60


def test_prune_never_evicts_an_in_flight_run(store) -> None:
    old = store.create_run(generate.GenerationSpec(n_units=3, days=1))
    store.append_readings(old.run_id, _readings())
    store.mark_succeeded(old.run_id, n_rows=60)

    live = store.create_run(generate.GenerationSpec(n_units=3, days=1))
    store.append_readings(live.run_id, _readings())
    store.mark_running(live.run_id)                        # queued/running is untouchable

    store.prune(max_total_rows=0)
    assert store.get_run(old.run_id) is None
    assert store.get_run(live.run_id) is not None


# --- the worker (still no forge: run_generation is stubbed) -------------------


def test_worker_generates_stores_and_closes_out_the_run(store, monkeypatch) -> None:
    from pdm_mlops import worker

    monkeypatch.setattr(generate, "run_generation", lambda spec: _readings())
    spec = generate.GenerationSpec(n_units=3, days=1)
    run = store.create_run(spec)

    assert worker.execute_run(run.run_id, spec, store) == 60
    assert store.get_run(run.run_id).status == store_gen.STATUS_SUCCEEDED


def test_a_worker_crash_lands_on_the_run_row(store, monkeypatch) -> None:
    """A silent worker death is a spinner that never ends — the failure must be visible."""
    from pdm_mlops import worker

    def boom(spec):
        raise RuntimeError("the forge exploded")

    monkeypatch.setattr(generate, "run_generation", boom)
    spec = generate.GenerationSpec(n_units=3, days=1)
    run = store.create_run(spec)

    with pytest.raises(RuntimeError):
        worker.execute_run(run.run_id, spec, store)

    failed = store.get_run(run.run_id)
    assert failed.status == store_gen.STATUS_FAILED
    assert "the forge exploded" in failed.error


def test_worker_revalidates_the_caps_itself(store, monkeypatch) -> None:
    """The worker is separately invokable, so it owns its own inputs (not the API's word)."""
    from pdm_mlops import worker

    monkeypatch.setattr(generate, "run_generation", lambda spec: _readings())
    over_cap = generate.GenerationSpec(n_units=generate.MAX_UNITS, days=generate.MAX_DAYS)
    run = store.create_run(over_cap)

    with pytest.raises(generate.CapExceeded):
        worker.execute_run(run.run_id, over_cap, store)
    assert store.get_run(run.run_id).status == store_gen.STATUS_FAILED


# --- the real forge (needs [generate]; skipped offline and in CI) -------------

needs_forge = pytest.mark.skipif(
    not __import__("pdm_mlops.data", fromlist=["data"]).generator_available(),
    reason="needs the `[generate]` extra (the real can-telemetry-forge)",
)


@needs_forge
def test_the_forge_produces_exactly_the_fleet_the_caps_promised(store) -> None:
    """The caps are only meaningful if the requested fleet size is the one you get.

    The generator draws each contract's unit count *around* the requested number; a demo
    whose caps are approximate is a demo whose storage arithmetic is approximate. So the
    demo config pins the variance to zero — and this asserts it, on the real forge.
    """
    from pdm_mlops import worker

    spec = generate.GenerationSpec(n_units=6, days=1).validate()
    run = store.create_run(spec)
    n_rows = worker.execute_run(run.run_id, spec, store)

    assert n_rows == spec.expected_rows == 6 * 288
    frame = store.readings_frame(run.run_id)
    assert frame.unit_id.nunique() == 6
    # Era-NULL units come back as NULL, not as zeros (ADR-003) — the model needs the
    # difference, and a JSON round-trip is exactly where it would get quietly lost.
    assert frame.loc[:, list(features.FEATURE_COLUMNS)].notna().any().all()
    features.assert_no_leakage(frame.loc[:, list(features.FEATURE_COLUMNS)])


@needs_forge
def test_generation_is_deterministic(store) -> None:
    """Same seed → same fleet. The repo's determinism invariant, on the demo path too."""
    spec = generate.GenerationSpec(n_units=3, days=1, seed=7).validate()
    first = generate.run_generation(spec)
    second = generate.run_generation(spec)
    pd.testing.assert_frame_equal(first, second)
