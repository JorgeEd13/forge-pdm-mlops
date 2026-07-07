"""F2 training-layer tests — the model-selection-as-MLOps-process contract.

All offline: the data source is the committed fixture (passed in, so the
full-regeneration path is never touched), and MLflow points at a per-test tmp file
backend (no server, no network). These assert the F2 DoD: **two tracked runs**, a
**registered winner**, and **same-seed-same-metric determinism**.
"""

from __future__ import annotations

import mlflow
import pytest
from mlflow.tracking import MlflowClient

from pdm_mlops import config, data, sequence, train


# The default seed (42) holds out fixture units that happen to have zero failures, so
# its test split is single-class and ROC-AUC is undefined there (see
# test_degenerate_fixture_split_is_rejected). These tracking tests use a seed whose
# unit-grouped split is class-rich on both sides — only an issue on the tiny fixture;
# the full 134-unit dataset is fine at the default seed (ADR-003).
FIXTURE_SEED = 0


@pytest.fixture(scope="module")
def fixture_readings():
    return data.load_fixture()


@pytest.fixture
def tmp_tracking(tmp_path):
    """A throwaway SQLite MLflow backend so runs/registry never leak between tests."""
    return config.sqlite_tracking_uri(tmp_path / "mlflow.db")


def test_train_tracks_both_models_and_registers_winner(fixture_readings, tmp_tracking) -> None:
    summary = train.train(
        seed=FIXTURE_SEED, tracking_uri=tmp_tracking, readings=fixture_readings, register=True
    )

    # Two contenders, both tracked with a run id and a real ROC-AUC.
    assert {r.name for r in summary.results} == {"logreg", "lightgbm"}
    assert all(r.run_id for r in summary.results)
    assert all(0.0 <= r.metric <= 1.0 for r in summary.results)

    # The winner is the best-scoring run.
    assert summary.winner.metric == max(r.metric for r in summary.results)

    # MLflow actually recorded two runs in the experiment.
    client = MlflowClient(tracking_uri=tmp_tracking)
    exp = client.get_experiment_by_name(config.EXPERIMENT_NAME)
    runs = client.search_runs([exp.experiment_id])
    assert len(runs) == 2
    for run in runs:
        assert config.PRIMARY_METRIC in run.data.metrics

    # The winner is registered in the model registry.
    assert summary.registered_version is not None
    versions = client.search_model_versions(f"name='{config.REGISTERED_MODEL_NAME}'")
    assert len(versions) == 1
    assert versions[0].version == summary.registered_version


def test_no_register_still_tracks_but_skips_registry(fixture_readings, tmp_tracking) -> None:
    summary = train.train(
        seed=FIXTURE_SEED, tracking_uri=tmp_tracking, readings=fixture_readings, register=False
    )
    assert summary.registered_version is None
    client = MlflowClient(tracking_uri=tmp_tracking)
    assert client.search_model_versions(f"name='{config.REGISTERED_MODEL_NAME}'") == []


def test_same_seed_same_metric(fixture_readings, tmp_path) -> None:
    a = train.train(
        seed=FIXTURE_SEED,
        tracking_uri=config.sqlite_tracking_uri(tmp_path / "a.db"),
        readings=fixture_readings,
        register=False,
    )
    b = train.train(
        seed=FIXTURE_SEED,
        tracking_uri=config.sqlite_tracking_uri(tmp_path / "b.db"),
        readings=fixture_readings,
        register=False,
    )
    metrics_a = {r.name: r.metric for r in a.results}
    metrics_b = {r.name: r.metric for r in b.results}
    assert metrics_a == metrics_b
    assert a.winner.name == b.winner.name


def test_train_defaults_to_data_loader_when_no_readings(monkeypatch, tmp_tracking) -> None:
    # With no explicit frame, train() must pull from the data layer (the real path
    # in production). We inject the fixture via the `load` hook to stay offline.
    called = {"n": 0}

    def fake_load():
        called["n"] += 1
        return data.load_fixture()

    summary = train.train(
        seed=FIXTURE_SEED, tracking_uri=tmp_tracking, register=False, load=fake_load
    )
    assert called["n"] == 1
    assert len(summary.results) == 2


def test_degenerate_fixture_split_is_rejected(fixture_readings, tmp_tracking) -> None:
    # A unit-grouped split can hold out only never-fail units → single-class test set →
    # ROC-AUC undefined. train() must fail loudly, not log a meaningless nan metric.
    # We construct that condition deterministically (one failing unit + several healthy
    # ones, seed 0) rather than lean on a fixture quirk: the multi-mode fixture is now
    # class-rich at every seed (ADR-019), so no seed triggers it on the full fixture.
    group = sequence.GROUP_COLUMN
    fails = fixture_readings.loc[fixture_readings[config.TARGET] == 1, group].unique().tolist()
    healthy = [u for u in fixture_readings[group].unique() if u not in fails]
    degenerate = fixture_readings[fixture_readings[group].isin(fails[:1] + healthy[:6])]
    with pytest.raises(train.DegenerateSplit):
        train.train(seed=0, tracking_uri=tmp_tracking, readings=degenerate, register=False)


# --- F2.6: tuned params + watchers flow through train() ---------------------


def test_train_consumes_tuned_params_on_cleaned_frame(fixture_readings, tmp_tracking) -> None:
    # Passing `tuned` overrides must (a) build the tuned models and (b) default to the
    # cleaned frame (the tuner searched on it). A non-default override should change the
    # logged params, proving it threaded through.
    tuned = {"lightgbm": {"n_estimators": 150, "num_leaves": 20}}
    summary = train.train(
        seed=FIXTURE_SEED,
        tracking_uri=tmp_tracking,
        readings=fixture_readings,
        register=False,
        tuned=tuned,
    )
    assert len(summary.results) == 2
    client = MlflowClient(tracking_uri=tmp_tracking)
    exp = client.get_experiment_by_name(config.EXPERIMENT_NAME)
    runs = {r.data.tags.get("mlflow.runName"): r for r in client.search_runs([exp.experiment_id])}
    lgbm = runs["lightgbm"]
    assert lgbm.data.params["n_estimators"] == "150"
    assert lgbm.data.params["tuned"] == "True"
    assert lgbm.data.params["cleaned_inputs"] == "True"


def test_audit_raises_when_a_watcher_trips(monkeypatch, fixture_readings, tmp_tracking) -> None:
    # Force the majority-baseline watcher to trip by pinning the test score to 0.5; with
    # audit=True and strict semantics, train() must surface the FitAudit, not swallow it.
    from pdm_mlops import diagnostics

    monkeypatch.setattr(train, "_score", lambda model, ds: 0.5)
    with pytest.raises(diagnostics.FitAudit):
        train.train(
            seed=FIXTURE_SEED,
            tracking_uri=tmp_tracking,
            readings=fixture_readings,
            register=False,
            audit=True,
        )
