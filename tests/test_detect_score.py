"""F2.5 scoring-harness tests — the ground-truth table is honest and tie-aware.

The harness is the one place labels are read (to *grade* detectors). These tests assert
the grades are well-formed and that the tie-aware alarm budget does not let a sparse
detector "win" by flagging everything.
"""

from __future__ import annotations

import numpy as np
import pytest

from pdm_mlops import data, detect_score


@pytest.fixture(scope="module")
def readings():
    return data.load_fixture()


def test_score_ladder_grades_each_cheap_rung(readings) -> None:
    score = detect_score.score_ladder(readings, include_autoencoder=False)
    names = {r.name for r in score.rungs}
    assert {"multivariate", "temporal"} <= names
    assert "autoencoder" not in names
    for r in score.rungs:
        assert 0.0 <= r.average_precision <= 1.0
        assert np.isnan(r.roc_auc) or 0.0 <= r.roc_auc <= 1.0
        for fam, rec in r.family_recall.items():
            assert np.isnan(rec) or 0.0 <= rec <= 1.0


def test_alarm_set_is_tie_aware_for_sparse_scores() -> None:
    # A mostly-zero detector with 1 % ones must NOT alarm every row under a 2 % budget:
    # the budget quantile lands on the zero floor; tie-awareness keeps only the ones.
    scores = np.zeros(1000)
    scores[:10] = 1.0  # 1 % positives
    alarmed = detect_score._alarm_set(scores, budget=0.02)
    assert alarmed.sum() == 10  # exactly the ones, not all 1000


def test_alarm_set_respects_budget_for_dense_scores() -> None:
    rng = np.random.default_rng(0)
    scores = rng.random(1000)
    alarmed = detect_score._alarm_set(scores, budget=0.05)
    # within one row of the 5 % budget (rounding at the boundary)
    assert abs(alarmed.sum() - 50) <= 1


def test_recall_nan_when_family_absent(readings) -> None:
    rng = np.random.default_rng(0)
    fake = rng.random(len(readings))
    labels = readings[[detect_score.LABEL_OUTLIER, detect_score.LABEL_FAMILY]]
    rs = detect_score.score_rung("x", fake, labels, families=["a_family_not_present"])
    assert np.isnan(rs.family_recall["a_family_not_present"])


def test_missing_label_columns_fail_loud(readings) -> None:
    with pytest.raises(KeyError, match="ground-truth column"):
        detect_score.score_ladder(readings.drop(columns=[detect_score.LABEL_OUTLIER]))


def test_format_table_is_readable(readings) -> None:
    score = detect_score.score_ladder(readings, include_autoencoder=False)
    text = detect_score.format_ladder_score(score)
    assert "ROC-AUC" in text and "multivariate" in text and "temporal" in text
