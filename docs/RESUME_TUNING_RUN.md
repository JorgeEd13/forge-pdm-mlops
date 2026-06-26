# Resume: F2.6 HPO gain measurement (baseline vs tuned)

Scratch handoff for finishing the "real gains of F2.6" measurement on the **notebook**
(faster i7/16GB, Linux — avoids the desktop launch stall below). Delete this file once
the result is folded into `docs/STATE.md` / ADR-006.

## Goal

Measure whether F2.6's Optuna HPO actually moves the model AUC on the **refreshed 0.2.0
data**, vs the default-params baseline, **on the same cleaned frame** (apples-to-apples:
only the hyper-parameters differ). Decided with Jorge: fresh baseline + tuned on the same
regenerated data, not vs stale registered metrics.

## Baseline — DONE on the desktop (2026-06-26, seed 42)

Command (cleaned 0.2.0 frame, default params):

    pdm train --clean --audit --diagnose --seed 42

Result (held-out, unit-disjoint test ROC-AUC):

| model    | test ROC-AUC | notes                          |
|----------|--------------|--------------------------------|
| lightgbm | **0.8118**   | winner; registered v3          |
| logreg   | **0.7131**   |                                |

- `--audit` passed (no `FitAudit`): on the full 134-unit split neither model tripped the
  overfit-gap or majority watchers. The fixture-only overfit trip (ADR-006) did **not**
  fire on full data, as expected.
- This confirms the STATE's "~0.82" claim for the data refresh (ADR-020) at the model layer.

## Tuned — NOT YET RUN. Do this on the notebook.

    pdm train --tune --audit --diagnose --seed 42

This runs the grouped-CV Optuna study (40 trials × 2 models, `DEFAULT_TRIALS=40`) on the
cleaned frame, then trains the tuned winner. The `--tune` path forces the cleaned frame
(`clean` defaults True when `tuned` given), so it matches the baseline above.

Capture stdout — the final block prints both `format_tune` (searched grouped-CV scores +
winning params) and the `format_summary` comparison table (the tuned test AUCs).

### Then compare

For each model, `tuned_test_auc − baseline_test_auc`:
- lightgbm vs 0.8118
- logreg  vs 0.7131

Per-model grouped-CV AUC + train AUC are logged on each MLflow run (`cv_roc_auc`,
`train_roc_auc`) under experiment `forge-pdm-failure` — pull them to see if the
train/CV/test story changed, not just the headline number.

## ⚠️ Desktop launch stall — root cause (so we don't repeat it)

The tuned run was launched on the desktop as a **Bash background task piped through
`tee`**, and it **wedged at process startup for 90 minutes at 0 CPU-seconds / 0 MB
working set** — it never executed a single trial (or even the first `print`). Confirmed
by `Win32_Process` CPU/WS sampling; a working LightGBM study would show hundreds of
CPU-seconds.

Isolation test: the **same venv python launched natively via PowerShell ran in 2.2s.**
So the block is the **Git Bash background-task + `tee` pipe** interacting badly with the
starved 2-core i3 I/O — **not** a code, data, MLflow-lock, or generator-regen bug. The
baseline (which ran fine) happened to get through; the heavier launch wedged.

If you ever must run it on the desktop again: launch **natively (PowerShell, no Bash
pipe, no `tee`)**, redirect to a file with `*> log.txt` instead, and lower `--trials`
(e.g. 15 — the synthetic score plateaus fast per ADR-006, 15 trials is enough to detect
whether HPO moves it). On the notebook none of this matters — just run the command.

## Environment facts (both machines)

- Package + deps already present in the desktop `.venv` at `JORGE/.venv/`:
  `can-telemetry-forge==0.2.0` (pinned, refreshed generator), mlflow 3.14, optuna 4.9,
  lightgbm 4.6, matplotlib 3.10, torch 2.12. On the notebook, `pip install -e '.[tune,generate]'`
  (and `[deep]` if you want the AE rung) reproduces it; the pin guarantees byte-identical
  regenerated data (ADR-001).
- Full dataset regenerates from `configs/dataset.json` (seed 42, ~3.47M rows × 134 units).
- MLflow backend is local SQLite at `mlruns/mlflow.db` (ADR-004). The desktop DB already
  has the baseline runs (incl. registered `forge-pdm-failure-classifier` v3). On the
  notebook you'll get a fresh local DB — that's fine, you only need the tuned numbers to
  compare against the baseline table above.

## Doc reconciliation still pending (after the tuned number is in)

The STATE has an unresolved tension to close once we know the tuned AUC:
- ADR-006 / `tune.py` docstring say HPO is "instrumentation, not an accuracy play... the
  score stays ~0.55 by design."
- The later data-refresh note says that ~0.55 framing is **superseded** (real ≈0.82 now)
  and that "HPO does not move, the DATA moved."

The tuned run **empirically settles** whether HPO moves it on the new data. Update the
"Honesty note (carries forward)" in STATE and the `tune.py` docstring's "faint by design"
line to match the measured delta (whichever way it lands — a near-zero delta is itself the
honest, postable result that confirms the instrumentation framing on realistic data).
