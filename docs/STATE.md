# State — forge-pdm-mlops

Updated: 2026-06-26

## Current focus

**F2.5 (Outlier robustness — clean first) — DONE.** A three-rung detection ladder
(`detect.py`) — multivariate (IsolationForest + robust Mahalanobis), temporal
(exact-value freeze runs + sustained monotone creep, with the detectable signals chosen
**unsupervised** at fit time), and a CPU-only PyTorch autoencoder — each **scored against
the generator's ground-truth labels** (`detect_score.py`), which are read in exactly one
place to *grade* (and to tune the temporal constants), never as a detector input or a
model feature (the ADR-003 leakage guard holds, asserted by test). The autoencoder
**earns its place** (best overall, beats the cheap rungs on subtle recall). The temporal
rung is a **deliberate rewrite**: its first rolling-variance/slope form scored ~0.02 F1
and flagged >85 % of rows; the diagnosis became the fix, and the negative result is
logged in ADR-005. Output = a leakage-safe **`signal_suspect`** feature
(`suspect.add_signal_suspect`, opt-in via `features.prepare(suspect_feature=True)`) + a
**data-quality watcher** (`suspect.data_quality_check`, forensic-watcher pattern, doubles
as an F5 drift signal). `pdm detect` prints the scored table. New `[deep]` torch extra,
out of core CI. **49 tests green offline** (27 from F0–F2 + 22 new). ADR-005.

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

### F2.5 — Outlier robustness (2026-06-26)

- **`detect.py`** — the ladder behind one `Detector` surface (`fit`/`score` → a
  `[0,1]` suspicion per row, **label-free**): `MultivariateDetector` (IsolationForest +
  `MinCovDet` Mahalanobis, `support_fraction=0.9`), `TemporalDetector` (exact-value
  freeze runs ≥ `STUCK_MIN_RUN` on *unsupervised-selected* continuous signals + a
  sustained monotone-creep window on *non-monotone* signals), `AutoencoderDetector`
  (CPU torch, reconstruction error). `build_ladder`/`fit_score_all`.
- **`detect_score.py`** — reads the labels (the **only** place) to grade each rung:
  ROC-AUC + AP vs. `is_outlier`, per-`anomaly_type` recall at a **tie-aware** top-2%
  alarm budget (`_alarm_set` — a sparse detector can't win by flagging everything), and
  the **autoencoder-earns-its-place** verdict. `pdm detect` prints the table.
- **`suspect.py`** — `add_signal_suspect` (combines rungs → the leakage-safe
  `signal_suspect` column, re-passes the guard) wired into `features.prepare(
  suspect_feature=True)`; `data_quality_check` forensic watcher (raises
  `DataQualitySpike` in strict mode; F5 drift signal).
- **The temporal rewrite (ADR-005).** Rolling-variance/slope → stuck/drift scored ~0.02
  F1 and flagged >85% of rows; rewritten to the freeze-run / monotone-creep signatures
  with unsupervised signal eligibility → stuck recall ≈0.59 @ precision ≈0.64 on
  native-res, drift high-precision/low-recall. The negative result is documented, not
  hidden. Thresholds auto-derived vs. ground truth (with Jorge) then pinned.
- **`[deep]` extra** (CPU `torch`), kept out of core CI. **ADR-005.**
- **Tests** — `test_detect.py` (12: ranges, label-free proof, determinism, unsupervised
  signal selection, no-flag-everything, `_equal_run_lengths`, AE skip-if-no-torch),
  `test_detect_score.py` (6: grades well-formed, tie-aware budget dense+sparse, NaN for
  absent family, loud on missing labels), `test_suspect.py` (6: feature leakage-safe,
  `prepare` wiring, determinism, watcher fires + strict raises). **49 total green.**

## Next step (concrete)

**F2.6 — Tune + diagnose (instrumentation).** On the **cleaned** F2.5 inputs (train with
`features.prepare(suspect_feature=True)` so `signal_suspect` is in the matrix), make
"why this model, with these params" visible and defensible. **Not** an accuracy play —
on this synthetic data the score is faint by design; the value is the tracked search +
diagnostics + guards.

- **HPO (`tune.py`)** — an **Optuna** study (small budget ~30–50 trials, seeded) over
  each model's param space, scored by **unit-grouped CV** (`GroupKFold`) so the search
  can't leak units across folds and undo ADR-003. Logged to MLflow; `pdm tune` runs it;
  tuned params feed `train`. New `[tune]` extra (`optuna`).
- **Diagnostics** — per fitted model, log to its MLflow run: a learning curve, feature
  importance (LightGBM gain / LogReg |coef| — `signal_suspect` should rank), a
  calibration check, and a precision/recall threshold sweep. Artifacts, not just scalars.
- **Training watchers** (forensic pattern): `DegenerateSplit` (shipped F2), an
  **overfit-gap** guard (train − CV AUC over a threshold), and a **majority-baseline**
  guard (test AUC must beat the majority class). Opt-in `--audit`.
- **ADR-006** — HPO method, the diagnostics set, the watcher policy, the honesty note.

**Honesty note:** neither F2.5 cleaning nor F2.6 HPO will move the ~0.547 AUC much on
this synthetic data — the value is the *visible, ground-truth-scored process + the
guards*, not accuracy.

One phase per session — F2.5 closes at this boundary for review before F2.6 starts.

## Notes

- No GPU, no paid services, no training tokens — local NumPy/pandas + cheap models;
  MLflow on a local file backend; CI is free and offline.
- Determinism is a hard invariant: one seed → data → split → train, same metrics.
- The fixture vs. the full dataset distinction is load-bearing (ADR-001): clone-and-
  run convenience without ever reporting metrics off the reduced slice.
