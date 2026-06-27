"""F2.7 temporal-ladder tests — apples-to-apples split, causal no-leak, determinism.

Offline on the committed smoke fixture. The torch-free rungs (the shared split, the
temporal features, the windowing) test on every run; the TCN rung needs the ``[deep]``
torch extra and skips cleanly when it is absent (CI never installs it). The reported
number is the GPU run; here the TCN is a tiny CPU model purely to prove the contract.
"""

from __future__ import annotations

import mlflow
import numpy as np
import pytest
from mlflow.tracking import MlflowClient

from pdm_mlops import config, data, features, sequence

# Like the F2 tests: the default seed (42) holds out fixture units with zero failures, so
# its test split is single-class on the tiny fixture. Use a seed whose unit-grouped split
# is class-rich on both sides — the full 134-unit dataset is fine at the default (ADR-003).
FIXTURE_SEED = 0
TINY_WINDOW = 6


def _has_torch() -> bool:
    try:
        import torch  # noqa: F401
    except Exception:
        return False
    return True


needs_torch = pytest.mark.skipif(not _has_torch(), reason="needs the [deep] torch extra")


@pytest.fixture(scope="module")
def readings():
    return data.load_fixture()


@pytest.fixture
def tmp_tracking(tmp_path):
    return config.sqlite_tracking_uri(tmp_path / "mlflow.db")


def _tiny_tcn(seed: int = FIXTURE_SEED):
    # A deliberately tiny CPU model: the test asserts the *contract*, not the accuracy.
    return sequence.TCNClassifier(
        window=TINY_WINDOW, channels=4, layers=2, epochs=2, batch_size=512,
        seed=seed, device="cpu",
    )


# --------------------------------------------------------------------------- #
# The shared split is the EXACT F1 split (same test rows → apples-to-apples)    #
# --------------------------------------------------------------------------- #


def test_split_indices_match_features_prepare(readings) -> None:
    train_idx, test_idx = sequence.split_indices(readings, seed=FIXTURE_SEED)
    ds = features.prepare(readings, seed=FIXTURE_SEED)

    # Identical held-out units and identical held-out rows (so the ROC-AUC is comparable).
    y = readings[config.TARGET].to_numpy()
    np.testing.assert_array_equal(y[test_idx], ds.y_test.to_numpy())
    np.testing.assert_array_equal(y[train_idx], ds.y_train.to_numpy())
    assert set(readings[sequence.GROUP_COLUMN].iloc[test_idx]) == set(ds.groups_test)


def test_split_is_unit_disjoint(readings) -> None:
    train_idx, test_idx = sequence.split_indices(readings, seed=FIXTURE_SEED)
    units = readings[sequence.GROUP_COLUMN].to_numpy()
    assert not (set(units[train_idx]) & set(units[test_idx]))


# --------------------------------------------------------------------------- #
# Rung (b) temporal features: leakage-safe and strictly causal                  #
# --------------------------------------------------------------------------- #


def test_temporal_features_are_leakage_safe(readings) -> None:
    feat = sequence.temporal_features(readings, window=TINY_WINDOW)
    # The guard must pass — no target/label-side column may appear.
    features.assert_no_leakage(feat)
    assert not (set(feat.columns) & set(features.LEAKY_COLUMNS))
    # Raw signals are carried plus 4 engineered stats each.
    assert len(feat.columns) == len(sequence.SIGNAL_COLUMNS) * 5
    assert len(feat) == len(readings)


def test_temporal_features_do_not_peek_into_the_future(readings) -> None:
    base = sequence.temporal_features(readings, window=TINY_WINDOW)

    # Corrupt a FUTURE row of one unit; an EARLIER row's causal features must not move.
    df = readings.copy().reset_index(drop=True)
    unit = df[sequence.GROUP_COLUMN].iloc[0]
    rows = df.index[df[sequence.GROUP_COLUMN] == unit].to_numpy()
    rows = rows[np.argsort(df[sequence.TIME_COLUMN].to_numpy()[rows])]
    early, late = rows[2], rows[-1]
    df.loc[late, "engine_speed_rpm"] = df.loc[late, "engine_speed_rpm"] + 9999.0

    bumped = sequence.temporal_features(df, window=TINY_WINDOW)
    np.testing.assert_allclose(
        base.iloc[early].to_numpy(), bumped.iloc[early].to_numpy(), equal_nan=True
    )
    # And the future row itself *did* change (sanity: the corruption took effect).
    assert not np.allclose(
        base.iloc[late].to_numpy(), bumped.iloc[late].to_numpy(), equal_nan=True
    )


# --------------------------------------------------------------------------- #
# Windowing: causal, unit-bounded, left-padded, mask covers padding/era-NULL    #
# --------------------------------------------------------------------------- #


def test_windows_are_causal_and_unit_bounded(readings) -> None:
    train_idx, _ = sequence.split_indices(readings, seed=FIXTURE_SEED)
    w = sequence.build_windows(readings, window=TINY_WINDOW, train_idx=train_idx)
    s = np.arange(len(w.win_idx))[:, None]
    # No window entry references a future row (> current), and none crosses below the unit.
    assert (w.win_idx <= s).all()
    # Each row's window references exactly one unit (its own) in sorted (contiguous) space.
    units_sorted = readings[sequence.GROUP_COLUMN].to_numpy()[
        np.argsort(w.inv, kind="stable")
    ]
    gathered_units = units_sorted[w.win_idx]
    cur_units = units_sorted[:, None]
    # Only valid (non-pad) positions must match the current unit.
    same = (gathered_units == cur_units) | (w.win_valid == 0.0)
    assert same.all()


def test_left_pad_positions_are_zeroed_in_every_channel(readings) -> None:
    w = sequence.build_windows(readings, window=TINY_WINDOW)
    # The very first sorted row of a unit has only itself → window is all left-pad but the
    # last slot. Gather it and assert the padded timesteps are zero across all channels.
    first_rows = np.where(w.win_valid[:, 0] == 0.0)[0][:5]
    if len(first_rows) == 0:
        pytest.skip("no short-history rows in this fixture slice")
    batch = sequence._gather_batch(w, first_rows)  # (B, 2C, W)
    for b, row in enumerate(first_rows):
        pad_slots = np.where(w.win_valid[row] == 0.0)[0]
        assert np.all(batch[b, :, pad_slots] == 0.0)


# --------------------------------------------------------------------------- #
# Rung (c) the TCN: deterministic, and the three-way comparison contract        #
# --------------------------------------------------------------------------- #


@needs_torch
def test_tcn_is_deterministic(readings) -> None:
    train_idx, test_idx = sequence.split_indices(readings, seed=FIXTURE_SEED)
    y = readings[config.TARGET].astype("int8").to_numpy()
    a = _tiny_tcn().fit(readings, train_idx, y[train_idx]).predict_proba(readings, test_idx)
    b = _tiny_tcn().fit(readings, train_idx, y[train_idx]).predict_proba(readings, test_idx)
    np.testing.assert_allclose(a, b)
    assert a.shape == test_idx.shape
    assert (a >= 0.0).all() and (a <= 1.0).all()


@needs_torch
def test_tcn_scores_every_test_row(readings) -> None:
    # Every held-out row gets a score (short histories left-padded) — no subset advantage.
    _, test_idx = sequence.split_indices(readings, seed=FIXTURE_SEED)
    y = readings[config.TARGET].astype("int8").to_numpy()
    train_idx, _ = sequence.split_indices(readings, seed=FIXTURE_SEED)
    proba = _tiny_tcn().fit(readings, train_idx, y[train_idx]).predict_proba(readings, test_idx)
    assert len(proba) == len(test_idx)
    assert np.isfinite(proba).all()


@needs_torch
def test_compare_runs_three_rungs_on_same_test_rows(readings, tmp_tracking) -> None:
    cmp = sequence.compare(
        readings,
        seed=FIXTURE_SEED,
        tracking_uri=tmp_tracking,
        tcn=_tiny_tcn(),
        register=True,
    )
    # Three rungs, each with a real ROC-AUC and a tracked run id.
    assert {r.name for r in cmp.results} == {"lightgbm_perrow", "lightgbm_temporal", "tcn"}
    assert all(0.0 <= r.metric <= 1.0 for r in cmp.results)
    assert all(r.run_id for r in cmp.results)

    # The verdict is reported either way and is internally consistent.
    temporal = next(r.metric for r in cmp.results if r.name == "lightgbm_temporal")
    tcn = next(r.metric for r in cmp.results if r.name == "tcn")
    assert cmp.tcn_earns_its_place == (tcn > temporal)
    assert cmp.margin_over_temporal == pytest.approx(tcn - temporal)

    # The winner is the best-scoring rung and is registered.
    assert cmp.winner.metric == max(r.metric for r in cmp.results)
    assert cmp.registered_version is not None

    # MLflow actually recorded three runs in the shared experiment.
    client = MlflowClient(tracking_uri=tmp_tracking)
    exp = client.get_experiment_by_name(config.EXPERIMENT_NAME)
    runs = client.search_runs([exp.experiment_id])
    assert len(runs) == 3
    for run in runs:
        assert config.PRIMARY_METRIC in run.data.metrics


@needs_torch
def test_compare_is_deterministic(readings) -> None:
    a = sequence.compare(readings, seed=FIXTURE_SEED, tcn=_tiny_tcn(), log_mlflow=False)
    b = sequence.compare(readings, seed=FIXTURE_SEED, tcn=_tiny_tcn(), log_mlflow=False)
    am = {r.name: r.metric for r in a.results}
    bm = {r.name: r.metric for r in b.results}
    for name in am:
        assert am[name] == pytest.approx(bm[name])
