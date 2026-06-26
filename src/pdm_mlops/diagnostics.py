"""Model diagnostics + training watchers — make "why this model" visible (F2.6).

Two roles, both opt-in from ``train(audit=True)`` (ADR-006):

1. **Diagnostics** (:func:`log_diagnostics`) — per fitted model, write *artifacts* (not
   just scalars) to its MLflow run so the fit is inspectable after the fact: a learning
   curve, feature importance (LightGBM gain / LogReg ``|coef|`` — ``signal_suspect``
   should rank), a calibration check, and a precision/recall threshold sweep. Each is a
   small CSV (always) and a PNG (when matplotlib is present) — CSVs keep CI artifact-light
   and the data reproducible; the PNGs are the human-readable version.

2. **Training watchers** (:func:`audit_fit`, the forensic-watcher pattern): cheap guards
   that fail **loud** on a suspicious fit rather than letting a misleading number through —
   an **overfit-gap** guard (train − CV AUC over a threshold) and a **majority-baseline**
   guard (test AUC must beat the trivial majority-class predictor, AUC 0.5). They return a
   structured report and raise in ``strict`` mode. `DegenerateSplit` (train.py, F2) is the
   third watcher in this family.

Everything is deterministic and offline. matplotlib is an optional nicety: absent, the
numeric CSVs still land, so diagnostics never become a hard dependency.
"""

from __future__ import annotations

import csv
import tempfile
from dataclasses import dataclass
from pathlib import Path

import mlflow
import numpy as np
import pandas as pd
from sklearn.metrics import precision_recall_curve, roc_auc_score

from . import config, features, models

#: Train − CV AUC above this is flagged as overfitting by :func:`audit_fit`. Generous:
#: a small train/CV gap is normal; this catches a model that has memorised the train units.
OVERFIT_GAP_LIMIT: float = 0.15

#: A model must beat this AUC (the majority-class baseline is exactly 0.5) by at least this
#: margin, or :func:`audit_fit` flags it as no better than trivial.
MAJORITY_AUC: float = 0.5
MAJORITY_MARGIN: float = 0.005


# --- Diagnostics (artifacts) ------------------------------------------------


def _try_savefig(fig, path: Path) -> None:
    """Save a matplotlib figure if it was created (no-op when matplotlib is absent)."""
    if fig is not None:
        fig.savefig(path, dpi=90, bbox_inches="tight")
        import matplotlib.pyplot as plt

        plt.close(fig)


def _matplotlib():
    """Return the pyplot module, or ``None`` if matplotlib is not installed."""
    try:
        import matplotlib

        matplotlib.use("Agg")  # headless; no display on the server/CI
        import matplotlib.pyplot as plt

        return plt
    except Exception:
        return None


def _write_csv(path: Path, header: list[str], rows: list[tuple]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def feature_importance(model: models.Model, feature_names: list[str]) -> list[tuple[str, float]]:
    """(feature, importance) pairs, descending — LightGBM gain or LogReg ``|coef|``.

    For LogReg the estimator is a pipeline; the linear coefficients live on its final
    ``clf`` step and are taken in absolute value (scaled inputs → comparable magnitudes).
    """
    est = model.estimator
    if model.name == "lightgbm":
        importances = np.asarray(est.feature_importances_, dtype=float)
    else:
        clf = est.named_steps["clf"]
        importances = np.abs(np.asarray(clf.coef_, dtype=float)).ravel()
    pairs = list(zip(feature_names, importances.tolist()))
    return sorted(pairs, key=lambda p: p[1], reverse=True)


def _threshold_sweep(y_true: pd.Series, proba: np.ndarray) -> list[tuple[float, float, float]]:
    """(threshold, precision, recall) along the PR curve — the operating-point trade."""
    precision, recall, thresholds = precision_recall_curve(y_true, proba)
    # precision_recall_curve returns one fewer threshold than precision/recall points.
    return [
        (float(t), float(p), float(r))
        for t, p, r in zip(thresholds, precision[:-1], recall[:-1])
    ]


def _calibration(y_true: pd.Series, proba: np.ndarray, *, n_bins: int = 10) -> list[tuple[float, float, int]]:
    """(mean predicted prob, observed failure rate, count) per probability bin."""
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.clip(np.digitize(proba, bins) - 1, 0, n_bins - 1)
    out: list[tuple[float, float, int]] = []
    y = np.asarray(y_true)
    for b in range(n_bins):
        mask = idx == b
        n = int(mask.sum())
        if n == 0:
            continue
        out.append((float(proba[mask].mean()), float(y[mask].mean()), n))
    return out


def log_diagnostics(model: models.Model, ds: features.Dataset) -> None:
    """Write the diagnostic artifacts for ``model`` to the active MLflow run.

    Must be called inside an open ``mlflow.start_run``. Writes CSVs always (numeric,
    reproducible, CI-light) and PNGs when matplotlib is present. Uses the held-out test
    split for calibration / threshold sweep, and the fitted model for importances.
    """
    proba = model.predict_proba(ds.X_test)
    plt = _matplotlib()

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)

        # 1. Feature importance.
        fi = feature_importance(model, ds.feature_names)
        _write_csv(d / "feature_importance.csv", ["feature", "importance"], fi)
        fig = None
        if plt is not None:
            fig, ax = plt.subplots(figsize=(6, 3.5))
            names = [f for f, _ in fi][::-1]
            vals = [v for _, v in fi][::-1]
            ax.barh(names, vals)
            ax.set_title(f"{model.name} — feature importance")
            fig.tight_layout()
        _try_savefig(fig, d / "feature_importance.png")

        # 2. Calibration.
        cal = _calibration(ds.y_test, proba)
        _write_csv(d / "calibration.csv", ["mean_pred", "observed_rate", "count"], cal)
        fig = None
        if plt is not None and cal:
            fig, ax = plt.subplots(figsize=(4, 4))
            ax.plot([0, 1], [0, 1], "--", color="grey", linewidth=1)
            ax.plot([c[0] for c in cal], [c[1] for c in cal], "o-")
            ax.set_xlabel("mean predicted")
            ax.set_ylabel("observed rate")
            ax.set_title(f"{model.name} — calibration")
        _try_savefig(fig, d / "calibration.png")

        # 3. Precision/recall threshold sweep.
        sweep = _threshold_sweep(ds.y_test, proba)
        _write_csv(d / "threshold_sweep.csv", ["threshold", "precision", "recall"], sweep)
        fig = None
        if plt is not None and sweep:
            fig, ax = plt.subplots(figsize=(5, 3.5))
            ax.plot([s[0] for s in sweep], [s[1] for s in sweep], label="precision")
            ax.plot([s[0] for s in sweep], [s[2] for s in sweep], label="recall")
            ax.set_xlabel("threshold")
            ax.legend()
            ax.set_title(f"{model.name} — precision/recall vs. threshold")
        _try_savefig(fig, d / "threshold_sweep.png")

        # 4. Learning curve (AUC vs. training-set fraction, grouped-CV-free quick view).
        lc = _learning_curve(model, ds)
        _write_csv(d / "learning_curve.csv", ["train_fraction", "train_auc", "test_auc"], lc)
        fig = None
        if plt is not None and lc:
            fig, ax = plt.subplots(figsize=(5, 3.5))
            ax.plot([p[0] for p in lc], [p[1] for p in lc], "o-", label="train")
            ax.plot([p[0] for p in lc], [p[2] for p in lc], "o-", label="test")
            ax.set_xlabel("training fraction")
            ax.set_ylabel(config.PRIMARY_METRIC)
            ax.legend()
            ax.set_title(f"{model.name} — learning curve")
        _try_savefig(fig, d / "learning_curve.png")

        mlflow.log_artifacts(str(d), artifact_path=f"diagnostics/{model.name}")


def _learning_curve(model: models.Model, ds: features.Dataset) -> list[tuple[float, float, float]]:
    """(fraction, train AUC, test AUC) over growing prefixes of the train split.

    A deliberately cheap learning curve: refit on the first ``frac`` of the (already
    unit-grouped) train rows and score train-vs-test. The gap closing or staying wide is
    the overfitting read; the absolute level shows the synthetic ceiling.
    """
    out: list[tuple[float, float, float]] = []
    n = len(ds.X_train)
    for frac in (0.25, 0.5, 0.75, 1.0):
        k = max(2, int(n * frac))
        Xk, yk = ds.X_train.iloc[:k], ds.y_train.iloc[:k]
        if yk.nunique() < 2 or ds.y_test.nunique() < 2:
            continue
        m = models.BUILDERS[model.name](seed=int(model.params.get("seed", config.DEFAULT_SEED)))
        m.estimator.fit(Xk, yk)
        tr = float(roc_auc_score(yk, np.asarray(m.estimator.predict_proba(Xk))[:, 1]))
        te = float(roc_auc_score(ds.y_test, np.asarray(m.estimator.predict_proba(ds.X_test))[:, 1]))
        out.append((float(frac), tr, te))
    return out


# --- Training watchers (forensic pattern) -----------------------------------


class FitAudit(RuntimeError):
    """Raised by :func:`audit_fit` in strict mode when a fit fails a watcher."""


@dataclass(frozen=True)
class AuditReport:
    """A fit's verdict from the training watchers."""

    name: str
    train_auc: float
    cv_auc: float
    test_auc: float
    overfit_gap: float
    overfit_tripped: bool
    beats_majority: bool

    @property
    def tripped(self) -> bool:
        return self.overfit_tripped or not self.beats_majority

    def summary(self) -> str:
        flags = []
        if self.overfit_tripped:
            flags.append(f"OVERFIT(gap={self.overfit_gap:.3f}>{OVERFIT_GAP_LIMIT})")
        if not self.beats_majority:
            flags.append(f"NO-LIFT(test_auc={self.test_auc:.3f}≤majority)")
        verb = "TRIPPED " + ",".join(flags) if flags else "ok"
        return (
            f"audit[{self.name}] [{verb}]: train={self.train_auc:.3f} "
            f"cv={self.cv_auc:.3f} test={self.test_auc:.3f}"
        )


def audit_fit(
    model: models.Model,
    ds: features.Dataset,
    *,
    cv_auc: float,
    test_auc: float,
    strict: bool = False,
) -> AuditReport:
    """Run the overfit-gap + majority-baseline watchers on a fitted model.

    ``cv_auc`` is the grouped-CV score (from F2.6 tuning, or recomputed); ``test_auc`` is
    the held-out score ``train`` already has. The train AUC is measured here on the fitted
    model. In ``strict`` mode a tripped watcher raises :class:`FitAudit`; otherwise the
    caller inspects ``.tripped``.
    """
    train_auc = float(
        roc_auc_score(ds.y_train, model.predict_proba(ds.X_train))
    )
    overfit_gap = train_auc - cv_auc
    overfit_tripped = overfit_gap > OVERFIT_GAP_LIMIT
    beats_majority = test_auc > MAJORITY_AUC + MAJORITY_MARGIN
    report = AuditReport(
        name=model.name,
        train_auc=train_auc,
        cv_auc=cv_auc,
        test_auc=test_auc,
        overfit_gap=overfit_gap,
        overfit_tripped=overfit_tripped,
        beats_majority=beats_majority,
    )
    if report.tripped and strict:
        raise FitAudit(report.summary())
    return report
