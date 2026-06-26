# Architecture Decision Records

Short, dated records of non-obvious choices. Newest at the bottom of each phase.

---

## ADR-001 — Data coupling: a pinned library dependency **and** a committed smoke fixture; models train on the full regenerated dataset only

**Date:** 2026-06-25 · **Phase:** F0 · **Status:** accepted

**Context.** This repo's data source is the companion `can-telemetry-forge`
generator. Two needs pull in opposite directions: (a) a portfolio reader should be
able to `git clone && pytest` and have CI pass **without** installing the generator
or hitting a network; (b) reported models must train on a **real, full** dataset,
and that dataset must be **identical across machines** (the desktop and the notebook
must never end up with subtly different data).

**Decision.**
1. The generator is a **pinned optional dependency** (`[generate]` extra,
   `can-telemetry-forge==0.2.0`). "Same config" only reproduces if the generator
   version matches too, so the version is pinned, not floating. (Bumped 0.1.0 → 0.2.0
   when the generator's ADR-020 added progressive pre-failure degradation, changing the
   data — a deliberate, pinned refresh; the fixture below was rebuilt against it.)
2. [`configs/dataset.json`](../configs/dataset.json) is the **single cross-machine
   source of truth** for the training dataset (seed, window, resolution, label
   horizon). Both machines regenerate the **full** dataset from this file + the
   pinned generator → byte-identical data.
3. A tiny **committed smoke fixture** (`data/sample_readings.parquet`, ~185 KB) lets
   the repo run/test offline. It is built by `scripts/build_sample.py` as a *strict
   reduction of the very same canonical config* (shorter window, ~hourly stride,
   stratified 20-unit subsample, modelling columns only, float32 + categoricals) so
   it can never silently diverge from the real data.

**The hard rule:** the fixture is for **plumbing/CI/offline smoke only — never a
training set.** `data.py` regenerates the full dataset from `configs/dataset.json`
for any real run, and falls back to the fixture **only** when the generator is
unavailable (and says so). No reported metric is ever computed on the reduced slice.

**Why (Jorge, 2026-06-25).** Keeping one config across setups prevents "a dataset on
the desktop and a completely different one on the notebook." Training on the
shrunk-to-fit-GitHub slice would report metrics off a toy dataset — dishonest and
non-representative. Splitting the two roles (fixture for offline plumbing, full
regeneration for real work) gives both clone-and-run convenience and honest results.

**Consequences.** CI is fast and network-free. The full dataset is never committed
(`.gitignore`), only its recipe. A generator-version bump is a deliberate, pinned
change that the fixture must be rebuilt against.

---

## ADR-002 — MLOps stack: MLflow + FastAPI + Evidently + **Prefect *and* GitHub Actions**

**Date:** 2026-06-25 · **Phase:** F0 · **Status:** accepted

**Context.** The project exists to close the **MLOps-in-production gate**:
experiment tracking, model registry, serving, drift monitoring, and orchestration.
Everything must run with **no GPU, no paid cloud, free CI**, on a modest desktop.

**Decision.** The spine is **MLflow** (experiment tracking + model registry, local
file backend — no server, no cost), **FastAPI** (serving the registered model),
**Evidently** (the drift report). For orchestration we deliberately use **two
layers that compose rather than duplicate**:

- **Prefect** *authors and executes* the pipeline as a real DAG —
  `detect_drift → [branch] → retrain → evaluate → promote` — with task
  dependencies, retries, and a run-graph UI. This closes the **named-orchestration**
  part of the gate (the thing recruiters mean by "orchestration": Airflow / Prefect
  / Dagster). It runs **in-process** for tests and local runs, so there is no
  mandatory server or daemon.
- **GitHub Actions** *schedules and triggers* that Prefect flow on a cron, on free
  cloud runners. This closes the separate **scheduled-cloud-execution** lacuna (the
  other lingering gate) on a genuine free tier, with nothing to pay for or babysit.

**Why both — and why this is architecture, not résumé-padding.** They sit at
**different layers**: Actions is the *scheduler/trigger*, Prefect is the *flow
author/executor*. Each closes a **distinct** gate.
- **Prefect only** would skip the free cloud-execution signal.
- **GitHub Actions only** would under-sell "orchestration" — a YAML file with a
  `cron:` reads as CI (which the other showcase repos already have), not as a DAG
  with dependencies and retries. The marquee drift→retrain loop *is* genuinely a
  branching DAG, so Prefect is the right tool, not decoration.
The composition is the real-world pattern (a scheduler kicking an orchestrator), so
documenting the "why both" here keeps it legible as a deliberate design choice.

**Model (see ADR-003 when written).** Deliberately cheap — a scikit-learn
`LogisticRegression` baseline and a LightGBM contender, compared *through MLflow*.
The model is not the point; the pipeline around it is.

**Consequences.** Every component is free and local-first. Tests run Prefect
in-process and MLflow against a tmp file backend, so CI needs no services. A future
hosted deploy (Fly.io / Render / HF Spaces) is a stretch goal (F6), not required —
the Actions run already demonstrates cloud execution.

---

## ADR-003 — Leakage-safe features: signals-only inputs, era-NULL kept, split BY UNIT

**Date:** 2026-06-26 · **Phase:** F1 · **Status:** accepted

**Context.** The generator's `readings` table carries, alongside the J1939 sensor
signals and the `failure_within_h` target, several columns that *describe the failure
itself* — `failure_mode`, `anomaly_type`, `is_outlier`. It also has era-gated
missingness (whole units are NULL for sensors their era never had, e.g. EGT/DEF on a
pre-emissions machine), and each unit's rows are an autocorrelated time series. Three
ways to quietly get a dishonestly good score, so three deliberate guards.

**Decision.**
1. **Features are sensor signals only.** `features.FEATURE_COLUMNS` is the eight
   J1939 channels. The target and the label-side bookkeeping (`failure_mode`,
   `anomaly_type`, `is_outlier`) are listed in `LEAKY_COLUMNS` and **excluded** — they
   are knowable only *because* the failure already occurred. `assert_no_leakage` runs
   on every prepared frame and **raises** if any of them appears in `X`, so a future
   rename/edit can't silently leak the answer (it's tested, not just documented).
2. **Era-NULL missingness is preserved, not imputed.** The feature layer does **no**
   imputation: NaN is a real, informative pattern (which sensor era a unit belongs
   to). LightGBM consumes NaN natively; the LogReg pipeline imputes at *model* time
   (F2), keeping the feature frame faithful to the source.
3. **The train/test split is BY UNIT.** `GroupShuffleSplit` on `unit_id` (seeded,
   `test_size=0.25` of *units*) so no unit appears in both sides. A random row split
   would scatter one unit's autocorrelated series across the boundary and inflate the
   score; grouping keeps generalization-to-new-machines honest. Disjointness is
   asserted, not assumed.

**Why.** All three are the difference between a number that looks good and a number
that means something. The leakage guard and the unit-grouped split are exactly the
"did you actually evaluate this honestly?" questions a reviewer asks of a PdM model;
making them explicit, tested invariants is the portfolio point.

**Consequences.** Determinism holds end to end: same `readings` + seed → same split →
(F2) same metrics. Verified on the full regenerated dataset (134 units → 100 train /
34 test, disjoint, comparable failure rates) and on the offline fixture in CI.

---

## ADR-004 — Train as a tracked two-model comparison; local **SQLite** MLflow backend; cloudpickle sklearn artifacts

**Date:** 2026-06-26 · **Phase:** F2 · **Status:** accepted

**Context.** F2 is the MVP core. The point is **not** a good model — it is to show
*model selection as an MLOps process*: two candidates, both tracked, the winner chosen
on a recorded metric and registered, all reproducible. Three concrete choices fell out
of building it on MLflow 3.

**Decision.**
1. **Two contenders behind one interface, compared through MLflow.** `models.py`
   exposes a `LogisticRegression` pipeline (median-impute → scale → logreg) and a
   `LGBMClassifier`, both as a `Model` with the same `fit`/`predict_proba`.
   Imputation lives **in the LogReg pipeline**, not the feature layer, so era-NULL
   stays intact upstream (ADR-003) and only the model that needs it fills it; LightGBM
   sees the NaN natively. `train.py` loops over both, opens an **MLflow run** each
   (params + the primary metric + the fitted model artifact), picks the best
   `roc_auc`, and **registers** it. "Which model is current and on what evidence" is
   recorded, not folklore.
2. **The local backend is SQLite, not the bare file store.** ADR-002 promised a
   server-free, cost-free local backend. MLflow 3 put the `./mlruns` *file store* into
   maintenance mode (it raises unless `MLFLOW_ALLOW_FILE_STORE=true`). Rather than
   opt back into a deprecated path, tracking + the **model registry** use a local
   **SQLite** DB (`mlruns/mlflow.db`) with a plain `mlartifacts/` artifact dir — still
   just files on disk, no daemon, no service. Tests point it at a tmp SQLite URI.
3. **sklearn artifacts are logged with cloudpickle.** MLflow 3's newer `skops`
   default refuses to serialise the LogReg pipeline (it flags `numpy.dtype` as an
   untrusted type). The sklearn flavor is pinned to `SERIALIZATION_FORMAT_CLOUDPICKLE`,
   which round-trips the full `Pipeline` faithfully for our own reload.

**A real guard that surfaced here — `DegenerateSplit`.** On the *full* dataset the
unit-grouped split is class-rich on both sides. On the tiny **smoke fixture**, holding
out a few units by group can land an **all-negative** test set, where ROC-AUC is
genuinely undefined. `train._score` **raises `DegenerateSplit`** instead of logging a
meaningless `nan` (which would also collide on a SQLite UNIQUE constraint in the
registry). The fixture's F2 tests use a seed whose split is two-class; one test asserts
the guard fires on the degenerate seed. This is a fixture-only artifact, not a
production one — but failing loudly beats a silent `nan`.

**Why.** All three keep the project's promises intact under a newer MLflow: honest
local-first MLOps (SQLite is still server-free), a faithful artifact round-trip, and a
metric that is either real or an explicit error — never a silent `nan`.

**Consequences.** `pdm train` produces **two tracked runs + a registered winner** and
is deterministic (same seed → same metrics). The backend is a single SQLite file +
an artifact dir, both git-ignored. F3 builds promotion/rollback on this same registry.

---

## ADR-005 — Outlier robustness: a ground-truth-scored detection ladder; signals-only/score-only honesty; `signal_suspect` feature; the temporal-rung rewrite; the autoencoder earns its place

**Date:** 2026-06-26 · **Phase:** F2.5 · **Status:** accepted

**Context.** The companion generator deliberately injects nine defect families, split
into **obvious** (`obvious_outlier` — one signal out of range) and **subtle**
(`joint_outlier`: each signal plausible, the *combination* not; `sensor_stuck`: an
in-range value that freezes; `sensor_drift`: a slow creep; the `can_frame_*` faults). A
serious PdM model has to survive these, and **cleaning before tuning** is the right ML
order — so robustness is F2.5 and HPO is F2.6 (it tunes on the cleaned frame). This
pillar is only *measurable* because the generator labels the outliers: the two repos
tell one story — the generator plants and labels the dirt, the MLOps side proves it
cleans it.

**Decision.**

1. **A detection ladder, each rung scored against ground truth** (`detect.py` +
   `detect_score.py`). Cheap → adaptive:
   - **Multivariate** — `IsolationForest` + a robust-covariance (`MinCovDet`,
     `support_fraction=0.9`) **Mahalanobis** distance over the signal vector. Catches
     the joint outlier a per-column check misses (700 rpm at 95 % load). The two views
     are min-max'd and combined by elementwise max.
   - **Temporal** — per-unit detection of `sensor_stuck` and `sensor_drift`.
   - **Autoencoder** — a small CPU-only PyTorch AE; reconstruction error = suspicion.
     New `[deep]` extra, **kept out of core CI**.

2. **The hard honesty rule — signals-only detection, score-only labels.** Every rung
   runs on the **feature signals only**; the `is_outlier` / `anomaly_type` labels are
   read in exactly one place — the scoring harness — to *grade* the detectors (and to
   *tune* the temporal constants). They are **never** a detector input or a model
   feature. A test (`test_detectors_are_label_free`) asserts that dropping the label
   columns does not change any score. The F1 leakage guard (ADR-003) stays sacred.

3. **The temporal rung is a from-scratch rewrite — and *why* matters.** The first
   attempt (rolling **variance** → stuck, rolling **slope** → drift on a robust-z
   scale) **failed**: even with thresholds tuned against the labels, its best F1 was
   **~0.02** (precision ~1 %) and it flagged **>85 % of rows**. The diagnosis was the
   fix: a *stuck* sensor is not "low variance" (a running engine's other signals keep
   moving and drown it) — it is **one signal repeating its exact value** while the rest
   move; a *drift* is not "a steep window" — it is a **sustained monotonic creep**.
   Detecting those signatures directly, with the detectable signals chosen
   **unsupervised at fit time** (continuous = near-zero baseline exact-repeat rate;
   drift-eligible = low baseline monotone-window fraction, so always-monotone channels
   like DEF level can't read as "drifting forever"), turned it into a real detector:
   on native-resolution data, **stuck recall ≈ 0.59 at precision ≈ 0.64**, drift caught
   at **high precision / low recall** (a clean ramp, few false alarms — the right bias
   for a feature). The honest cap: stuck recall ~0.6 because some freezes hit a
   non-continuous channel, and drift is genuinely the hardest family. **The negative
   result and its repair are part of the portfolio point**, not hidden.

4. **The thresholds are auto-derived from ground truth, then pinned.** The temporal
   constants (`STUCK_MIN_RUN`, `DRIFT_WINDOW`, `DRIFT_MONOTONE_FRAC`, the eligibility
   bounds) were swept against the labelled data for the best precision/recall trade and
   frozen as module constants — labels *tune* the detector, they never become an input.

5. **A tie-aware alarm budget in the scoring harness.** Per-family recall is measured
   at a fixed top-2 % alarm budget so a flag-everything rung can't "win". A naive budget
   quantile lands on a sparse detector's zero floor and would alarm every row; the
   tie-aware `_alarm_set` credits a 0.6 %-flagging detector with exactly those rows.

6. **The autoencoder earns its place.** Scored head-to-head like the cheap rungs, the AE
   is the **best overall** detector (AP ≈ 0.62–0.65 vs. multivariate ≈ 0.24) and
   **beats the best cheap rung on mean subtle recall** — so it stays. Had it not, the
   harness would have reported that plainly (`autoencoder_earns_place`). Each rung also
   owns a *different* family — multivariate the joints, temporal the freezes, the AE the
   broad/subtle shapes — a complementary ensemble, not redundancy.

7. **Output = a leakage-safe `signal_suspect` feature + a data-quality watcher.**
   `suspect.add_signal_suspect` combines the rungs (mean; cheap rungs by default so the
   feature needs no torch) into one `[0,1]` suspicion column appended to the feature
   matrix (`features.prepare(suspect_feature=True)`), re-passing the leakage guard so it
   is *proven* label-free. `suspect.data_quality_check` is a forensic watcher
   (the project's watcher pattern): it fails **loud** (raises in `strict` mode) when a
   batch's suspect rate spikes past a fitted baseline — a sensor bank going bad, or, in
   F5, the drifted season inflating outliers. It doubles as a drift signal for F5.

**Why.** F2.5 is where the project earns the word "robust" honestly: not by asserting
the pipeline handles dirty data, but by **scoring detectors against labelled dirt,
reporting what each does and doesn't catch, repairing a rung that didn't work and saying
so, and shipping the result as a leakage-safe feature**. The synthetic data makes the
ground truth available; the discipline is in using it only to grade and tune, never to
cheat.

**Consequences.** `pdm detect` prints the scored ladder table on the live dataset.
`signal_suspect` is available to F2.6's tuned models. The data-quality watcher is reused
as an F5 drift signal. The autoencoder lives behind the optional `[deep]` extra; the
cheap rungs (and the feature) run with zero extra dependencies, so CI stays offline and
torch-free. 22 new offline tests (49 total green).

---

## ADR-006 — Tune & diagnose: a unit-grouped Optuna study on the cleaned frame, diagnostics-as-artifacts, training watchers — instrumentation over accuracy

**Date:** 2026-06-26 · **Phase:** F2.6 · **Status:** accepted

**Context.** F2 picks a winner on a recorded metric; F2.5 cleans the inputs and ships a
`signal_suspect` feature. F2.6 closes the modelling loop by making **"why this model,
with these params"** *visible and defensible* — a tracked search, inspectable diagnostics,
and guards that fail loud on a misleading fit. The hard honesty caveat is stated up front:
on this synthetic data the score is **faint by design** (~0.547 AUC at F2), and neither
cleaning nor HPO moves it much. So F2.6 is explicitly **not an accuracy play** — the
portfolio value is the *process and the instrumentation*, not the number.

**Decision.**

1. **HPO = a seeded Optuna study, scored by unit-grouped CV** (`tune.py`). One study per
   contender (`models.BUILDERS`) over a restricted, declared tunable space
   (`LOGREG_TUNABLE` = `C`; `LIGHTGBM_TUNABLE` = tree budget + regularisation). The
   objective is the **mean ROC-AUC across `GroupKFold` folds of the *training* split** —
   whole units held out per fold, never a row, so the tuner **cannot leak a unit across
   folds and undo ADR-003**. The held-out test split is never seen by the search, so HPO
   never tunes against the number F2/F3 reports. The search runs on the **F2.5-cleaned
   frame** (`features.prepare(suspect_feature=True)`) so `signal_suspect` is in the matrix
   being optimised. Each study is logged to MLflow under a dedicated `-tune` experiment;
   tuned params feed `train(tuned=…)`. New `[tune]` extra (`optuna`, `matplotlib`).

2. **Tuned params are restricted overrides, validated, defaulting to the F2 baseline.**
   `build_logreg`/`build_lightgbm` take an `overrides` dict whose keys must be in the
   model's tunable set (`_check_overrides` **raises** on an unknown key rather than
   silently dropping it). With no overrides each builder is the **untuned F2 model
   exactly** — so F2 stays reproducible and F2.6 is a strict, legible superset.

3. **Diagnostics are artifacts, not just scalars** (`diagnostics.log_diagnostics`). Per
   fitted model: feature importance (LightGBM gain / LogReg `|coef|` — `signal_suspect`
   should rank), a calibration check, a precision/recall **threshold sweep**, and a cheap
   **learning curve**. Each is written as a **CSV always** (numeric, reproducible,
   CI-light) and a **PNG when matplotlib is present**. matplotlib is therefore an *optional
   nicety*: absent, the numeric CSVs still land, so diagnostics never become a hard
   dependency and CI stays light.

4. **Training watchers — the forensic-watcher pattern, opt-in `--audit`**
   (`diagnostics.audit_fit`). Two cheap guards that fail **loud** rather than let a
   misleading fit through: an **overfit-gap** guard (train − grouped-CV AUC over
   `OVERFIT_GAP_LIMIT` = 0.15 → the model memorised its train units) and a
   **majority-baseline** guard (test AUC must beat the trivial 0.5 majority-class
   predictor by a margin). They return a structured `AuditReport` and **raise `FitAudit`**
   in strict mode. `DegenerateSplit` (F2) is the third watcher in this family. Off by
   default so a normal `pdm train` is unchanged; `--audit` turns the loud guards on.

**Why.** The questions a reviewer asks of a tuned model are exactly these: *did the search
leak units? did you tune against the test set? is the model better than guessing, or just
overfit? what is it actually keying on?* F2.6 answers each as a **tested, tracked
invariant** — grouped-CV in the objective, the test split untouched, two watchers that
raise, and importance/calibration/threshold artifacts on every run — instead of asserting
"I tuned it." On synthetic data where the score can't impress, *that visible discipline is
the deliverable.*

**Consequences.** `pdm tune` produces a tracked Optuna study per model with grouped CV;
`pdm train --tune` chains the search into a tuned, cleaned-frame training run;
`pdm train --audit`/`--diagnose` add the guards/artifacts. The search is deterministic
(seeded TPE + order-deterministic `GroupKFold` → same best params). The `[tune]` extra is
optional and out of core CI (which trains untuned on the fixture); matplotlib is optional
within it. F3 builds gated promotion on the registered winner — tuned or not.
