# State — forge-pdm-mlops

Updated: 2026-06-26

## Current focus

**F2 (Train + track — MVP core) — DONE.** `pdm train` now runs an honest
**two-model comparison** (LogReg pipeline + LightGBM behind one interface), logs
**both** as MLflow runs (params + ROC-AUC + the fitted model artifact), picks the
winner by the primary metric, and **registers** it in the MLflow Model Registry —
all on a server-free local **SQLite** backend (ADR-004). Deterministic: same seed →
same metrics. **27 tests green offline** (15 from F0/F1 + 12 new). This is the MVP
core; F3 builds gated promotion/rollback on the same registry.

**F1 (Data + features) — DONE.** The data layer regenerates the **full** dataset
from the canonical config when the generator is present, and falls back **loudly**
to the committed smoke fixture offline (ADR-001). The feature layer turns `readings`
into a leakage-safe modelling frame: signals-only inputs with a tested leakage
guard, era-NULL missingness preserved (no imputation), and a deterministic
**unit-grouped** train/test split. ADR-003 records the three guards. (F0 — the
runnable skeleton — closed at an earlier boundary.)

## Done

- **Package skeleton** (`src/pdm_mlops/`): `__init__` (version only), `config.py`
  (paths, MLflow wiring, seeds, thresholds — one source of truth), `cli.py` (`pdm`
  with `--version` + `train`/`serve`/`flow`/`monitor` stubbed to point at their
  phase). `pyproject.toml` with core deps (numpy/pandas/pyarrow/scikit-learn/
  lightgbm/mlflow) + `dev`/`serve`/`ops`/`generate` extras; the `[generate]` extra
  pins `can-telemetry-forge==0.1.0`.
- **Canonical dataset config** (`configs/dataset.json`) — the single cross-machine
  source of truth (seed 42 / 90 days / 5min / 168h horizon). The full dataset
  regenerates identically on any machine from this + the pinned generator (ADR-001).
- **Offline smoke fixture** (`data/sample_readings.parquet`, ~185 KB) built by
  `scripts/build_sample.py` as a *strict reduction of the same canonical config*
  (14-day window, ~hourly stride, stratified 20-unit subsample, modelling columns,
  float32 + categoricals, zstd). 6,720 rows × 14 cols, 5.8% failure rate. **It is a
  smoke fixture only — never a training set** (ADR-001).
- **CI** (`.github/workflows/ci.yml`): Linux+Windows × 3.11/3.12, installs `[dev]`
  only, smoke-tests `pdm --version`, runs offline pytest. **`retrain.yml`**: the
  scheduled (cron) + manual cloud trigger surface for the F5 Prefect flow (a
  marked placeholder until F5).
- **Tests** (`tests/test_skeleton.py`): import, CLI help/stubs, fixture presence.
- **Docs**: CLAUDE.md, ROADMAP (F0–F6), ARCHITECTURE, DECISIONS (ADR-001/002),
  this STATE. `.gitignore` (mlruns, generated data, reports; un-ignores the fixture).
- **GitHub repo metadata applied** to `JorgeEd13/forge-pdm-mlops` (description + 14
  topics live in the About sidebar); command recorded in `.github/REPO_META.md`.
  The repo exists on GitHub with git initialized (branch `main`, remote set, no
  commits yet — F0 is the first commit).

### F1 — Data + features (2026-06-26)

- **`data.py`** — `load_readings()` prefers **full regeneration** from
  `configs/dataset.json` via the pinned generator (`regenerate_full`, with a
  `season` override hook for the F5 drift loop); falls back to the committed fixture
  with a **loud** `UserWarning` when the generator is absent, or raises
  `GeneratorUnavailable` if fallback is disabled. Verified on the real path: 90-day
  full dataset = **3.47M rows × 134 units** in ~98 s.
- **`features.py`** — `prepare()` returns a frozen `Dataset` (X/y/groups train+test).
  Inputs are the 8 J1939 signals only; `assert_no_leakage` (tested to fire) blocks
  the target + `failure_mode`/`anomaly_type`/`is_outlier`. **Era-NULL preserved** (no
  imputation). **Unit-grouped** `GroupShuffleSplit` (seeded, 25% of units), asserted
  disjoint — full data splits 100/34 units, fixture splits its 20 units cleanly.
- **Tests** — `test_data.py` (5: fixture load, fallback warns, season flagged,
  no-fallback raises, full-path-not-touching-fixture via monkeypatch) +
  `test_features.py` (6: signals-only/leakage-fires, unit-disjoint, determinism,
  seed-drives-partition, era-NULL preserved, binary target). All offline.
- **ADR-003** records the leakage guard / era-NULL / unit-split policy.

### F2 — Train + track (2026-06-26)

- **`models.py`** — two contenders behind one `Model` (`fit`/`predict_proba`):
  `build_logreg` (median-impute → scale → `LogisticRegression`, `class_weight=
  balanced`; imputation lives **in the pipeline** so era-NULL stays intact upstream)
  and `build_lightgbm` (`LGBMClassifier`, native NaN, no scaling). `build_all` returns
  them in fixed order. Each carries a flat `params` dict for MLflow.
- **`train.py`** — `train()` loops `build_all`, opens an **MLflow run** per model
  (params + ROC-AUC + the model artifact), picks the best `roc_auc`, and **registers**
  the winner. Local **SQLite** tracking/registry (`config.sqlite_tracking_uri`),
  injectable data source (`readings=`/`load=`) so tests stay offline. `_score` raises
  **`DegenerateSplit`** if the test set is single-class (a fixture-only artifact — the
  full split is class-rich) instead of logging a meaningless `nan`.
- **`pdm train`** — wired live (`--seed`, `--no-register`); prints a comparison table.
- **ADR-004** — two-model comparison; SQLite backend (MLflow 3 retired the bare file
  store); sklearn artifacts via cloudpickle (skops rejects `numpy.dtype`); the
  `DegenerateSplit` guard.
- **Tests** — `test_models.py` (7: both fit & emit proba, one interface, era-NULL fed
  in raw, LightGBM eats NaN, LogReg pipeline imputes, same-seed-same-fit) +
  `test_train.py` (5: two tracked runs + registered winner, `--no-register` skips the
  registry, same-seed-same-metric, defaults to the data loader, `DegenerateSplit`
  fires on the degenerate fixture seed). MLflow → tmp SQLite; all offline.

## Next step (concrete)

**F2.5 — Outlier robustness (clean first).** Re-scoped with Jorge (2026-06-26) after he
raised two things: (1) healthy skepticism about delegating ML judgment to me, and (2)
the generator **deliberately injects subtle, hard-to-spot outliers** the MLOps side must
handle. Clean-first is the right ML order (tuning on poisoned inputs is backwards), so
**outlier robustness becomes F2.5 and tuning/diagnostics moves to F2.6.**

Build a **detection ladder, each rung scored against the generator's ground-truth
labels** (`anomaly_type`/`is_outlier`): (a) **multivariate** — IsolationForest +
robust-covariance **Mahalanobis** (catches `joint_outlier`: each signal plausible, the
*combination* implausible — Jorge's exact framing, "together they're suspects"); (b)
**temporal** — per-unit rolling variance/slope for `sensor_stuck`/`sensor_drift`
(thresholds = a domain call WITH Jorge); (c) **autoencoder** — small CPU-only PyTorch
AE, reconstruction error = suspicion (Jorge chose to include it now; it must **earn its
place** — scored vs. ground truth and reported honestly whether it beats the cheap
rungs on *subtle* recall). Output = a **leakage-safe `signal_suspect` feature** (derived
from signals ONLY, scored against labels, never trained on the label — F1 leakage guard
stays sacred) + a **data-quality watcher**. ADR-005. New `[deep]` extra (CPU torch),
kept out of core CI. Likely 1–2 sessions. See ROADMAP F2.5.

**Then F2.6 — Tune + diagnose:** Optuna grouped-CV HPO on the cleaned inputs +
diagnostics + overfit/majority/degenerate watchers. ADR-006. **Honesty note:** on this
synthetic data neither cleaning nor HPO will move the ~0.547 AUC much — the value is the
*visible, ground-truth-scored process + the guards*, not accuracy.

One phase per session — F2 closes at this boundary for review before F2.5 starts.

## Notes

- No GPU, no paid services, no training tokens — local NumPy/pandas + cheap models;
  MLflow on a local file backend; CI is free and offline.
- Determinism is a hard invariant: one seed → data → split → train, same metrics.
- The fixture vs. the full dataset distinction is load-bearing (ADR-001): clone-and-
  run convenience without ever reporting metrics off the reduced slice.
