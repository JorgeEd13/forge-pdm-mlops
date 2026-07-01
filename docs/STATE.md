# State — forge-pdm-mlops

Updated: 2026-07-01

## Current focus

**F2.8 (Characterize the ceiling — is the limit the data or the model?) — BUILT, offline-tested
(full-data numbers pending a GPU `pdm ceiling` run). The capstone that closes the F2.* arc.**
The honest close of the F2.5→F2.7 investigation: stop *asserting* "0.82 is the data's
information limit" and **measure** it. New `ceiling.py`, three instruments, all on the **exact
F1 unit split / seed / test rows** and all **label-honest** (labels read only to grade/bound,
never as an honest feature — the ADR-003 / `detect_score` discipline):

1. **Decomposition** (`decompose`) — the honest per-row held-out AUC sliced by **time-to-failure
   horizon** (`[0,6)…[168,∞) h`) and by **failure mode**; each band's positives vs. all healthy
   rows, so the *shape* of predictability is visible (expected high near failure, fading far
   out — most of the 168 h window is healthy-and-unpredictable by construction). Time-to-failure
   is derived label-side (`time_to_failure`: event = one stride past a unit's last positive) and
   used only to bucket.
2. **Label-leaking upper-bound** (`upper_bound`) — a LightGBM that also sees `failure_mode` +
   the derived `time_to_failure_h`, bounding the **irreducible** error. A **fenced diagnostic**:
   own field, never a reported metric; the honest frames are asserted leak-free by
   `_assert_honest_frame` (a `LEAK_FEATURES` guard *on top of* `assert_no_leakage`, so even the
   non-target `time_to_failure_h` can't reach the honest path); the fence is asserted by test.
3. **Stacking redundancy probe** (`stacking_probe`) — an **OOF unit-grouped** (`GroupKFold`)
   LogisticRegression meta-learner over the base rungs. Can't beat its best base member ⇒ rungs
   are information-redundant ⇒ **the ceiling is the data, confirmed** (reported either way). A
   probe, **not a product** — a within-noise bump would only muddy the clean F2.7 finding.

**Compute / the TCN seam.** CPU-only on the low-end desktop (the two base rungs are the cheap
F2.7 LightGBM frames). The F2.7 **TCN** needs the GPU, so the probe takes its predictions
through `extra_oof={name: (oof_train, proba_test)}` — a notebook run folds the TCN's OOF in
**without a rewrite** (alignment asserted). Since F2.7 measured the TCN *below* rung (b), the
probe's **verdict** is already decidable from the two GBDT rungs; the TCN-included number is an
optional refinement. `pdm ceiling` runs it live; new `ceiling.py` + **13 tests** (offline,
deterministic). **ADR-010.** *(On the tiny fixture the numbers are artifacts — the reported
full-data decomposition/probe land on a GPU `pdm ceiling` run, recorded here at that boundary.)*

**F2.7 (Temporal modelling — does the trajectory help?) — DONE.** The honest follow-on to
the F2.6 HPO-null finding: if tuning is exhausted because the ceiling is a *representation*
limit (the failure is a degradation **ramp** — generator ADR-020 — and a per-row model
discards the trajectory), then the lever is temporal structure, **measured to earn its
place**. A **three-rung ladder** in new `sequence.py`, all on the **same unit split / seed /
metric / test rows** as F1 (proven row-identical by test). **Measured on the full data (GPU
RTX 4050, seed 42, deterministic across re-runs):**

| rung | ROC-AUC | Δ |
|---|---|---|
| (a) per-row LightGBM (the bar) | 0.8125 | — |
| (b) temporal-features LightGBM | **0.8194** | **+0.0069** vs. (a) |
| (c) dilated causal TCN | 0.8148 | **−0.0046** vs. (b) |

Two findings, both kept: **(1) temporal structure helps** (+0.0069 over per-row — the
trajectory carries signal, confirming the representation thesis), but the lift is modest
because the ramp is gentle; **(2) the deep model does NOT earn its place** — the TCN lands
below the cheap, interpretable temporal-features LightGBM, so rung (b) wins. The cheap rung
exists precisely to stop us conflating "temporal helps" with "deep helps", and it did its job
(the F2.5 autoencoder discipline again). The TCN geometry was fixed **a priori** and never
tuned against the reported test rows (that would be the very leakage the repo guards).
**HPO follow-up (measured, settles "could tuning the TCN win?"):** a seeded Optuna study (12
trials) over the TCN geometry, scored by **unit-grouped 3-fold CV on the *training* split
only** (test rows never seen), then evaluated once on the same held-out rows → tuned TCN
**0.8107**, *below* the a-priori TCN (−0.0041) and the temporal bar (−0.0087). **Tuning does
not rescue the deep model** — it sits within noise of the ≈0.82 data ceiling; the cheap rung
(b) keeps the title (doubly confirms the F2.6 HPO-null). The
causal-convolution no-future-leakage is **structural**; era-NULL enters as impute + a
missingness-mask channel; every test row scored (short histories left-padded). `pdm sequence`
runs it live; logged to the same MLflow experiment, winner registrable. New `sequence.py` +
10 tests. **73 tests green offline.** ADR-007.

**Data realism refresh — generator `can-telemetry-forge` 0.1.0 → 0.2.0 (cross-repo).**
Consuming the generator exposed that its failures had **no temporal signature** (a
failing unit's pre-failure rows were identical to its healthy rows), so a per-row model
scored ≈ 0.55 *by construction* — not for lack of modelling effort here. Fixed **in the
generator** (its ADR-020: a progressive pre-failure degradation ramp), not by tuning the
model against the data. Two changes landed on this side: (1) the `[generate]` pin moved
to `==0.2.0` and the committed smoke fixture was **rebuilt** against it; (2) `vibration_mms`
(the bearing signature, era-gated, previously unused) was added to `features.FEATURE_COLUMNS`
— it also flows into `detect.SIGNAL_COLUMNS` automatically. **Measured on a regenerated
dataset: ≈ 0.55 → 0.73 (ramp alone) → ≈ 0.82 (with vibration).** Two generator hazard
rebalances were prototyped, scored, and **rejected** (neither beat the ramp; logged in the
generator's ADR-020). The earlier "score is faint by design / ~0.55" framing in F2/F2.5/F2.6
is now **superseded** — the score is a real ≈ 0.82, earned from raw sensors with the
leakage guards intact. (F2–F2.6 process/instrumentation claims are unchanged and now sit on
honestly-learnable data.) Re-run `pdm train` to refresh the registered metrics.

**F2.6 (Tune + diagnose — instrumentation) — DONE.** On the F2.5-**cleaned** inputs
(`features.prepare(suspect_feature=True)`), "why this model, with these params" is now
**visible, tracked, and guarded** — deliberately *not* an accuracy play, now **measured**:
on the refreshed 0.2.0 data HPO moves the held-out test AUC by **+0.003 (lightgbm) / 0.000
(logreg)** — the real ≈0.82 came from the data (ADR-020), not the tuning (ADR-006). Three
pieces:

- **HPO (`tune.py`)** — one seeded **Optuna** study per contender over a *restricted,
  declared* tunable space (`models.LOGREG_TUNABLE`/`LIGHTGBM_TUNABLE`), scored by
  **unit-grouped `GroupKFold`** on the *training* split only, so the search can't leak a
  unit across folds (ADR-003 holds) and never tunes against the reported test number.
  Tracked to a `-tune` MLflow experiment; `pdm tune` runs it; tuned params feed
  `train(tuned=…)`. New `[tune]` extra (`optuna` + optional `matplotlib`).
- **Diagnostics (`diagnostics.log_diagnostics`)** — per fitted model, **artifacts** on its
  MLflow run: feature importance (`signal_suspect` ranks), calibration, a precision/recall
  threshold sweep, a learning curve. **CSV always** (CI-light, reproducible) + **PNG when
  matplotlib is present** — matplotlib is an optional nicety, never a hard dep.
- **Training watchers (`diagnostics.audit_fit`, opt-in `--audit`)** — the forensic-watcher
  pattern: an **overfit-gap** guard (train − grouped-CV AUC > 0.15) and a
  **majority-baseline** guard (test AUC must beat 0.5). Raise `FitAudit` in strict mode.
  `DegenerateSplit` (F2) is the third in this family.

**An honest finding, kept (not hidden).** On the **15-unit smoke fixture** the deep
LightGBM legitimately overfits — train AUC ≈ 1.0 vs. grouped-CV ≈ 0.60 → the overfit-gap
watcher **trips**. That is the watcher *earning its keep*, not a bug: grouped CV on so few
units is pessimistic by construction (the same fixture-size artifact ADR-004 records for
`DegenerateSplit`). A dedicated test asserts the trip on the fixture; the watcher's *pass*
path is tested on a shallow/regularised fit; the honest setting is the full 100-unit
dataset. **`pdm train --tune --audit --diagnose` chains all three live.** **63 tests green
offline** (49 from F0–F2.5 + 14 new). ADR-006.

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

### F2.6 — Tune + diagnose (2026-06-26)

- **`tune.py`** — `tune_model` runs a seeded **Optuna** study per contender over its
  restricted tunable space, objective = mean ROC-AUC over **unit-grouped `GroupKFold`**
  folds of the *training* split (single-class folds skipped; all-degenerate → `nan` →
  pruned). `tune` prepares the **cleaned** frame (`suspect_feature=True`), runs both
  studies, logs each to a `-tune` MLflow experiment, returns `{name: TuneResult}`.
  `DEFAULT_TRIALS=40`. The test split is never seen by the search.
- **`models.py`** — `build_logreg`/`build_lightgbm` take validated `overrides`
  (`_check_overrides` raises on an unknown key); with none they are the F2 baseline
  exactly. `BUILDERS` (name→builder) + `build_all(tuned=…)` feed tuned params by name.
- **`diagnostics.py`** — `log_diagnostics` writes feature-importance / calibration /
  threshold-sweep / learning-curve **artifacts** (CSV always + PNG if matplotlib) to the
  active run. `audit_fit` = overfit-gap (`OVERFIT_GAP_LIMIT=0.15`) + majority-baseline
  (`MAJORITY_AUC=0.5`) watchers → `AuditReport`, raise `FitAudit` in strict mode.
- **`train.py`** — gained `tuned=` / `clean=` (cleaned frame; defaults on when `tuned`
  given) / `audit=` (strict watchers) / `diagnose=` (artifacts); logs `cleaned_inputs`,
  `tuned`, and the audit's `cv_roc_auc`/`train_roc_auc`.
- **`cli.py`** — `pdm tune` (`--seed`, `--trials`) live; `pdm train` gained `--tune`
  (search then train tuned+cleaned), `--audit`, `--diagnose`, `--clean`.
- **`[tune]` extra** (`optuna` + optional `matplotlib`), out of core CI. **ADR-006.**
- **Tests** — `test_tune.py` (6: grouped CV never shares a unit, only-tunable params,
  tuned params build a model, determinism, one tracked run/model, searches the cleaned
  frame), `test_diagnostics.py` (7: importance ranks `signal_suspect`, artifacts land,
  watcher pass-path on a shallow fit, **fixture deep-fit trips overfit by design**,
  majority watcher fires + strict raises), + 2 in `test_train.py` (tuned params thread
  through on the cleaned frame; `--audit` raises). **63 total green.**

### F2.7 — Temporal modelling (2026-06-27)

- **`sequence.py`** — the three-rung ladder behind one comparison. `split_indices` mirrors
  `features.prepare`'s `GroupShuffleSplit` **bit-for-bit** (proven row-identical by test), so
  all rungs score on the *same* held-out rows. `temporal_features` builds per-unit **causal**
  rolling mean/std/slope/delta (groupby-rolling resets at each unit boundary → no future
  leak, no cross-unit bleed; re-passes `assert_no_leakage`). `build_windows` standardises on
  **train rows only**, imputes era-NULL to the train mean **and** emits a was-present mask
  channel, and pre-computes left-padded causal window indices (memory-bounded gather, not a
  materialised N×W×C tensor). `TCNClassifier` = a dilated **causal** 1-D conv stack
  (left-pad + right-chomp ⇒ the no-peek property is *structural*) → last-timestep head →
  per-row proba; `fit(readings, train_idx, y)` / `predict_proba(readings, idx)`. `compare`
  runs (a) per-row LightGBM, (b) temporal-features LightGBM, (c) the TCN, logs each to the
  **same** MLflow experiment, returns the three-way result + the earns-its-place verdict +
  registrable winner.
- **`cli.py`** — `pdm sequence` (`--window`, `--epochs`, `--channels`, `--device`,
  `--register`) live; the real path regenerates the full dataset, the TCN auto-selects CUDA.
- **Measured (full data, GPU RTX 4050, seed 42, deterministic across two re-runs):** per-row
  **0.8125** → temporal-features **0.8194** (**+0.0069**, temporal *does* help) → TCN
  **0.8148** (**−0.0046** vs. (b), deep does **not** earn its place). Cheap interpretable
  rung wins; verdict reported either way. TCN geometry fixed a-priori, **never** tuned vs. the
  test rows (leakage guard). **ADR-007** carries the table + the two honest findings.
- **`[deep]` torch extra reused** (no new dependency), out of core CI.
- **Tests** — `test_sequence.py` (10: split row-identical to F1, unit-disjoint, temporal
  features leakage-safe + **no future peek**, windows causal/unit-bounded, left-pad zeroed,
  TCN deterministic + scores every test row, three-rung compare on the same rows + registers,
  compare determinism). Torch-free rungs always run; TCN rungs skip without `[deep]`.
  **73 total green offline.**

### F2.8 — Characterize the ceiling (2026-07-01)

- **`ceiling.py`** — `characterize` runs three instruments on the shared honest base
  (`build_base`: the F2.7 per-row + temporal-features LightGBM frames on the **exact F1**
  unit split): `decompose` (held-out AUC by TTF-horizon bucket + by failure mode),
  `upper_bound` (a **fenced** label-leaking LightGBM diagnostic — `failure_mode` one-hot +
  derived `time_to_failure_h`), `stacking_probe` (OOF unit-grouped LogisticRegression over the
  rungs → beats-best-base verdict). `time_to_failure` derives the label-side TTF (event = one
  stride past a unit's last positive). `format_report` prints the capstone; `CeilingReport.
  ceiling_is_data` = the thesis flag (¬stack-beats-best-base).
- **The fence.** `_assert_honest_frame` = `features.assert_no_leakage` **plus** a
  `LEAK_FEATURES` guard, so even the non-target `time_to_failure_h` can never reach the honest
  path; the leaky frame is built only inside `upper_bound` and returned in its own field.
- **The TCN seam.** `stacking_probe(extra_oof={name: (oof_train, proba_test)})` folds a
  GPU-produced TCN OOF column in with no rewrite (alignment asserted). CPU-only otherwise.
- **`pdm ceiling`** (`--seed`, `--window`) live. No new dependency.
- **Tests** — `test_ceiling.py` (13: ttf finite exactly on positives + within horizon, base
  frames leak-free + exact-F1-split + unit-disjoint, decomposition covers horizon+modes with
  positives summing to held-out positives, upper-bound bounds & is fenced — **the fence fires**
  on `time_to_failure_h` and on target/label columns — determinism, the stacking seam folds in
  / rejects misaligned extra OOF). Offline, deterministic. **86 total green offline** (73 +
  13). **ADR-010.**
- **Pending:** the reported full-data numbers land on a GPU `pdm ceiling` run (fixture numbers
  are artifacts).

## Next step (concrete)

**F2.8 is built + offline-tested — the modelling investigation stops here by design.** One thing
outstanding on it: a **GPU `pdm ceiling` run on the full dataset** to record the reported
decomposition / upper-bound / probe numbers (the fixture numbers are artifacts). Optionally fold
the F2.7 TCN's OOF into the probe via the `extra_oof` seam on that same run. Not a blocker for F3.

**F3 — Registry + promotion is the concrete NEXT build (ADR-008). The MLOps spine the repo exists
to close.** Governed model lifecycle on the same MLflow SQLite registry F2 already writes to.
`registry.py`: register the winner, **stage→production promotion gated by the eval metric**, and
rollback. DoD: a *worse* candidate does **not** promote (asserted); rollback restores the prior
production version. Build it on the (tabular or temporal) winner F2.6/F2.7 produces; tests stay
offline (tmp SQLite, the fixture).

**Then F4 → F5 → F6 (the rest of the spine).** A complete
production spine (train → registry → serve → drift → retrain → cloud) is the rare portfolio
signal; finishing it beats polishing the modelling branch.

**F2.9 (RUL / graded label, ADR-011) and F2.10 (C-MAPSS, ADR-012) — FUTURE WORK, DEFERRED BY
DESIGN (2026-06-27, career-wide decision).** Both are scoped in full in `ROADMAP.md` but
intentionally **not built**: the rigor/honesty attitude is already proven (F2.5/2.6/2.7), an
over-deep F2 branch next to an unfinished gate *inverts* the signal, and RUL/C-MAPSS are a
deep-learning/benchmarking axis better owned by a dedicated DL showcase (or by making the private
[[project_fleet_ml]] browsable). Leaving them as **curated future work is itself the senior
signal** — knowing the next step and choosing the spine. Build only if a dedicated DL showcase is
decided.

**Honesty note (carries forward) — now measured, not asserted.** The real lift came from
the **data**, not the modelling: ADR-020's pre-failure degradation ramp + the
`vibration_mms` feature took the score ≈0.55→0.82. **F2.6 HPO, measured on the refreshed
0.2.0 data (notebook, seed 42, same cleaned frame), does *not* move it:** tuned − baseline
held-out test ROC-AUC = **+0.0034 (lightgbm 0.8118→0.8152) / 0.0000 (logreg
0.7131→0.7131)**; grouped-CV search 0.7526 / 0.6767; both pass `--audit` (lightgbm overfit
gap train−CV = 0.880−0.753 = 0.127 < 0.15; logreg train≈CV≈test). The value across F2–F2.6
is the *visible, ground-truth-scored process + the guards*, not accuracy — and the near-zero
HPO delta is itself the honest, postable confirmation of that on realistically learnable
data.

One phase per session — **F2.8 (characterize the ceiling, ADR-010) is built and offline-tested at
this boundary** (86 tests green on the i3), closing the F2.* modelling arc; F2.9 / F2.10 stay
deferred by design; **F3 (registry + gated promotion, ADR-008) is the next build** — the MLOps
gate the repo exists to close. Outstanding on F2.8: a GPU `pdm ceiling` full-data run for the
reported numbers (not a blocker for F3).

## Notes

- No GPU, no paid services, no training tokens — local NumPy/pandas + cheap models;
  MLflow on a local file backend; CI is free and offline.
- Determinism is a hard invariant: one seed → data → split → train, same metrics.
- The fixture vs. the full dataset distinction is load-bearing (ADR-001): clone-and-
  run convenience without ever reporting metrics off the reduced slice.
