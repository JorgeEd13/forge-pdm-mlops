"""F2.8 ceiling-characterization tests — the fence, the split, the probe, determinism.

Offline on the committed smoke fixture; no torch needed (the two base rungs are the
CPU-only LightGBM frames). The reported *numbers* are the GPU/full-data run — here we
assert the **contract**: labels never reach the honest path, the split is the exact F1
split, the decomposition covers the horizon, the upper-bound is fenced, and the stacking
seam aligns. Seed 0 (like the F2/F2.7 tests) gives a class-rich fixture test split.
"""

from __future__ import annotations

import numpy as np
import pytest

from pdm_mlops import ceiling, config, data, features, sequence

FIXTURE_SEED = 0
TINY_WINDOW = 6


@pytest.fixture(scope="module")
def readings():
    return data.load_fixture()


@pytest.fixture(scope="module")
def base(readings):
    return ceiling.build_base(readings, seed=FIXTURE_SEED, window=TINY_WINDOW)


# --------------------------------------------------------------------------- #
# time-to-failure is label-side and correct                                    #
# --------------------------------------------------------------------------- #


def test_ttf_finite_exactly_on_positives(readings):
    ttf = ceiling.time_to_failure(readings)
    finite = np.isfinite(ttf)
    pos = readings[config.TARGET].to_numpy() == 1
    # A finite time-to-failure marks exactly the positive (within-horizon) rows.
    assert np.array_equal(finite, pos)
    # The event is one stride ahead of the last positive row, so every ttf is positive.
    assert ttf[finite].min() > 0.0


def test_ttf_within_label_horizon(readings):
    ttf = ceiling.time_to_failure(readings)
    finite = ttf[np.isfinite(ttf)]
    # Every positive row is at most the label horizon (168 h) from its failure event.
    assert finite.max() <= 168.0 + 1e-6


# --------------------------------------------------------------------------- #
# the honest base is leakage-safe and on the exact F1 split                     #
# --------------------------------------------------------------------------- #


def test_base_frames_are_leak_free(base):
    # Neither honest frame carries the target/label-side columns nor the leak features.
    ceiling._assert_honest_frame(base.X_perrow)
    ceiling._assert_honest_frame(base.X_temporal)


def test_base_split_is_the_exact_f1_split(readings, base):
    tr, te = sequence.split_indices(readings, seed=FIXTURE_SEED)
    assert np.array_equal(base.train_idx, tr)
    assert np.array_equal(base.test_idx, te)
    # And unit-disjoint (the whole point of the grouped split).
    gtr = set(base.groups[base.train_idx])
    gte = set(base.groups[base.test_idx])
    assert gtr.isdisjoint(gte)


# --------------------------------------------------------------------------- #
# decomposition covers the horizon + modes, honestly                            #
# --------------------------------------------------------------------------- #


def test_decompose_covers_horizon_and_modes(base, readings):
    d = ceiling.decompose(base, readings, seed=FIXTURE_SEED)
    assert 0.0 <= d.overall <= 1.0
    # One horizon slice per configured bucket; positives sum to the held-out positives.
    assert len(d.by_horizon) == len(ceiling.TTF_BUCKETS_H) - 1
    y_test = base.y[base.test_idx]
    assert sum(s.n_positive for s in d.by_horizon) == int(y_test.sum())
    # Every failure mode present in the test positives is decomposed.
    modes_test = set(
        m for m in readings["failure_mode"].to_numpy()[base.test_idx].astype(str) if m and m != "nan"
    )
    assert {s.label for s in d.by_mode} == modes_test
    # A slice that is single-class reports None rather than a meaningless AUC.
    for s in d.by_horizon + d.by_mode:
        if s.roc_auc is not None:
            assert 0.0 <= s.roc_auc <= 1.0


# --------------------------------------------------------------------------- #
# the upper-bound is a FENCED diagnostic — it never touches the honest path     #
# --------------------------------------------------------------------------- #


def test_upper_bound_bounds_and_is_at_least_honest(base, readings):
    ub = ceiling.upper_bound(base, readings, seed=FIXTURE_SEED)
    # A model that also sees the label should never do *worse* than the honest one.
    assert ub.leaky >= ub.honest - 1e-9
    assert abs(ub.gap - (ub.leaky - ub.honest)) < 1e-12
    assert set(ub.leak_features) == set(ceiling.LEAK_FEATURES)


def test_honest_frame_rejects_leak_features(base):
    # The fence: if a leak feature reaches the honest path, it must fail loudly. The derived
    # ``time_to_failure_h`` is NOT in features.LEAKY_COLUMNS, so this exercises the ceiling
    # module's own LEAK_FEATURES guard (not just the upstream assert_no_leakage).
    bad = base.X_perrow.copy()
    bad["time_to_failure_h"] = 1.0
    with pytest.raises(ValueError, match="leak feature"):
        ceiling._assert_honest_frame(bad)
    # ...and the target/label-side columns are still rejected (by the upstream guard).
    bad2 = base.X_perrow.copy()
    bad2["failure_mode"] = "oil_starve"
    with pytest.raises(ValueError, match="leakage guard"):
        ceiling._assert_honest_frame(bad2)


def test_leak_features_are_label_side_only():
    # Every fenced leak feature is either a known label-side column or the derived TTF —
    # never one of the honest signal features. Guards against a careless rename.
    for col in ceiling.LEAK_FEATURES:
        assert col not in features.FEATURE_COLUMNS
    assert "failure_mode" in features.LEAKY_COLUMNS


# --------------------------------------------------------------------------- #
# the stacking probe: verdict either way + the TCN seam                         #
# --------------------------------------------------------------------------- #


def test_stacking_probe_reports_either_way(base):
    sp = ceiling.stacking_probe(base, seed=FIXTURE_SEED)
    assert set(sp.oof_columns) == {"lightgbm_perrow", "lightgbm_temporal"}
    assert sp.best_base in sp.base_test
    assert sp.best_base_auc == max(sp.base_test.values())
    assert abs(sp.margin - (sp.stack_auc - sp.best_base_auc)) < 1e-12
    assert sp.beats_best_base == (sp.margin > 0.0)


def test_stacking_seam_folds_in_extra_oof(base):
    # The TCN seam: an externally-produced OOF/test column is folded in with no rewrite.
    rng = np.random.default_rng(0)
    extra = {
        "tcn": (
            rng.random(len(base.train_idx)),
            rng.random(len(base.test_idx)),
        )
    }
    sp = ceiling.stacking_probe(base, seed=FIXTURE_SEED, extra_oof=extra)
    assert "tcn" in sp.oof_columns
    assert "tcn" in sp.base_test


def test_stacking_seam_rejects_misaligned_extra_oof(base):
    with pytest.raises(ValueError, match="misaligned"):
        ceiling.stacking_probe(
            base,
            seed=FIXTURE_SEED,
            extra_oof={"tcn": (np.zeros(3), np.zeros(len(base.test_idx)))},
        )


# --------------------------------------------------------------------------- #
# determinism + the full capstone                                              #
# --------------------------------------------------------------------------- #


def test_characterize_is_deterministic(readings):
    a = ceiling.characterize(readings, seed=FIXTURE_SEED, window=TINY_WINDOW)
    b = ceiling.characterize(readings, seed=FIXTURE_SEED, window=TINY_WINDOW)
    assert a.decomposition.overall == b.decomposition.overall
    assert a.upper_bound.leaky == b.upper_bound.leaky
    assert a.stacking.stack_auc == b.stacking.stack_auc
    assert a.stacking.margin == b.stacking.margin


def test_characterize_reports_all_three(readings):
    rep = ceiling.characterize(readings, seed=FIXTURE_SEED, window=TINY_WINDOW)
    assert rep.decomposition is not None
    assert rep.upper_bound is not None
    assert rep.stacking is not None
    # The thesis flag is the negation of the probe beating its best base.
    assert rep.ceiling_is_data == (not rep.stacking.beats_best_base)
    text = ceiling.format_report(rep)
    assert "VERDICT" in text and "upper-bound" in text and "DIAGNOSTIC" in text
