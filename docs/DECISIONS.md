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
**HPO is not an accuracy play** — the real lift came from the **data** (ADR-020's
pre-failure degradation ramp + the `vibration_mms` feature lifted the score ≈0.55→0.82),
not from tuning. This is now **measured, not asserted** (notebook run, seed 42, refreshed
0.2.0 data, same cleaned frame): tuned − baseline held-out test ROC-AUC = **+0.0034
(lightgbm: 0.8118→0.8152) / 0.0000 (logreg: 0.7131→0.7131)**. Grouped-CV search scores
0.7526 (lightgbm) / 0.6767 (logreg). So F2.6's portfolio value is the *process and the
instrumentation* — the visible, leakage-safe, ground-truth-scored search — not the number,
and the near-zero delta is itself the honest, postable confirmation of that on realistic
data.

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

---

## ADR-007 — Temporal modelling: a sequence contender (dilated **causal** TCN) added as a parallel, ground-truth-comparable rung, because the lever is representation, not tuning

**Date:** 2026-06-27 · **Phase:** F2.7 · **Status:** **accepted — built and measured** (2026-06-27)

**Context.** F2.6 **measured** that HPO does not move the score (+0.003 LightGBM / +0.000
LogReg on the full 0.2.0 data) — the per-row GBDT family is at its ceiling. That ceiling is
a *representation* limit, not a model-capacity one: the failure signal is a **progressive
pre-failure degradation ramp** over the horizon (the generator's ADR-020), and a model that
sees **one row** discards the trajectory that *is* the signal. The honest next lever is
therefore to give a model the temporal structure — not a bigger classifier or more trials.
This also fills a real portfolio gap: a **public** PyTorch deep-learning showcase (the only
existing sequence/encoder work, `project_fleet_ml`, is private/unbrowsable).

**Decision.**

1. **A three-rung honesty ladder, all on the same unit split / seed / metric / test rows**
   (apples-to-apples, the F2.5 ladder discipline):
   - **(a) per-row LightGBM** — the F2.6 ceiling (0.8152), the bar.
   - **(b) temporal-features LightGBM** — per-unit rolling/lag window stats (mean / slope /
     std over the recent window) fed to the *same* LightGBM. Isolates **"does temporal
     structure help at all"** with a cheap, CPU-only model — and becomes the bar the deep
     model must clear.
   - **(c) a PyTorch sequence model** — must **earn its place** over (b), measured and
     reported either way (exactly as the F2.5 autoencoder had to). Conflating "temporal
     helps" with "deep helps" is the trap rung (b) exists to avoid.

2. **Architecture = a 1-D dilated *causal* Temporal CNN (TCN)**, chosen for *this* problem,
   not for buzz:
   - **Multi-scale trend detection** via dilation is the cheapest way to capture a slope
     across a long window, and the most likely to extract signal the hand-crafted rolling
     features in (b) *cannot* (non-linear, cross-channel trajectory interactions) — which is
     exactly what "earning its place" requires.
   - **Causal padding structurally forbids future leakage**: the convolution at time *t*
     can only see *t* and earlier. The architecture *enforces* the no-peeking property the
     repo elsewhere guards by assertion — a stronger guarantee, not a weaker one.
   - **Determinism**: cuDNN's deterministic conv path is solid; recurrent (cuDNN RNN) and
     attention determinism on GPU is fiddlier. TCN is the best fit with our hard
     same-seed→same-metric invariant. (It still reads as an "encoder": the pooled conv
     stack is a learned temporal embedding → a small head.)

3. **Integration = a parallel contender, not a hack into `build_all`.** A new
   `sequence.py` builds per-unit, time-ordered windows by **reusing the exact F1 unit-grouped
   split** (same `GroupShuffleSplit` seed → the *same* train/test units as the tabular
   comparison) and wraps the TCN in the same `fit` / `predict_proba(→ per-row positive
   proba)` surface, so `train.py` can log it to the **same MLflow experiment**, score it by
   the same ROC-AUC **on the same test rows**, and let it compete for the registry. It is not
   forced through the signals-only tabular `Dataset` (whose `X` deliberately drops
   `unit_id`/time); windowing needs both, so it gets its own data path keyed off `readings`.

4. **era-NULL into a net = impute + a missingness-mask channel.** A neural net can't ingest
   NaN; rather than drop the era-NULL signal (ADR-003 keeps it as information), each signal
   is imputed *and* paired with a binary "was-present" channel, so "which sensor era this
   unit has" stays learnable — missingness-as-signal, carried into the sequence model.

5. **Every test row is scored** (left-pad short histories, the mask covers the padding) so
   the head-to-head metric is on the **identical** test set as the tabular rungs — no
   subset-mismatch advantage.

6. **Determinism & device.** `torch.use_deterministic_algorithms(True)` + seeded everything;
   the **tested** path is a tiny CPU model on the fixture (offline, deterministic, CI-safe);
   the **reported** number is the GPU run (RTX 4050, the notebook — `[[resources_compute]]`).
   Reuses the existing `[deep]` torch extra (no new dependency); falls back to CPU when CUDA
   is absent.

**Why.** The whole repo's thesis is *honest evaluation over a flashy number*. The senior move
after measuring that tuning is exhausted is not to abandon the number — it's to **identify the
right lever (representation), pull it, and measure whether it earned the complexity**. A TCN
that beats the temporal-features baseline is a legitimately higher number *and* a public deep
showcase; a TCN that doesn't is still the honest "I tried the right lever and reported it"
result. Either outcome is postable, and the causal-convolution leakage property is itself a CV
point.

**Consequences.** New `sequence.py` (windowing + TCN + the `fit`/`predict_proba` surface) and
a `pdm sequence` subcommand that logs the three rungs to the same MLflow experiment and lets
the winner — tabular or temporal — register. Tests stay offline/deterministic (a tiny CPU TCN
on the fixture; windowing leakage asserted; same-seed determinism; the shared split proven
row-identical to F1's). This is **F2.7**, a modelling phase that sits before F3 in execution;
F3 (gated promotion) consequently becomes **ADR-008**. (This note originally slated F5-drift
for ADR-009; that number was later taken by **F4 serving** once F4 needed its own ADR, so F5
drift is **ADR-013** — ADR-010 = F2.8, ADR-011/012 = the deferred F2.9/F2.10.) F2.7 does
not touch the MLOps gate work — F3/F4/F5 remain the priority that the repo exists to close.

**Outcome (measured — full data, GPU RTX 4050, seed 42, identical held-out rows).** The
three-way comparison, deterministic across re-runs:

| rung | ROC-AUC | vs. per-row | vs. temporal-features |
|---|---|---|---|
| (a) per-row LightGBM (the bar) | **0.8125** | — | — |
| (b) temporal-features LightGBM | **0.8194** | **+0.0069** | — |
| (c) dilated causal TCN (32 ch · 4 layers · window 24 · 12 epochs) | **0.8148** | +0.0023 | **−0.0046** |

Two honest findings, *both* kept:

1. **Temporal structure helps — a little.** Rung (b) clears the per-row bar by **+0.0069**.
   The trajectory carries signal a single row discards, confirming the representation thesis:
   it *was* the right lever (the ramp is a temporal pattern), even though the lift is modest
   because the failure ramp is gentle and a per-row snapshot already sees most of it.
2. **The deep model does NOT earn its place.** The TCN lands **−0.0046 below** the cheap,
   interpretable temporal-features LightGBM (and only +0.0023 over per-row). The cheap rung
   (b) exists precisely to forbid conflating "temporal helps" with "deep helps" — and it did
   its job: it won. This is the F2.5 autoencoder discipline applied again; the verdict is
   **reported plainly, not buried**.

Crucially, the TCN geometry was fixed **a priori** (a sensible default) and **not** tuned
against the reported test ROC-AUC — doing so would be exactly the test-set leakage the repo
guards everywhere. The causal-convolution no-future-leakage property (structural, not asserted)
and the row-identical shared split stand as the reusable CV points regardless of the number.

**HPO follow-up — measured, the verdict holds (full data, GPU, seed 42).** "Could tuning the
TCN beat the bar?" was settled empirically with the F2.6 `tune.py` discipline: a seeded Optuna
study (12 trials) over the TCN's geometry (`window`/`channels`/`layers`/`kernel`/`epochs`/`lr`/
`weight_decay`), scored by **unit-grouped 3-fold CV on the *training* split only** — the test
rows were never seen during the search. Best grouped-CV config (window 24 · 32 ch · 4 layers ·
kernel 2 · 4 epochs · lr 1.2e-3 · wd 2e-4) at CV-AUC **0.7887**; trained once on the full train
split and evaluated once on the same held-out rows → **test ROC-AUC 0.8107**, which is
**−0.0041 *below* the a-priori TCN (0.8148)** and **−0.0087 below the temporal-features bar
(0.8194)**. So **tuning does not rescue the deep model** — it lands within noise of the data
ceiling (≈0.82, the ramp / generator ADR-020), grouped-CV-best ≠ test-best, and the cheap
interpretable rung (b) keeps the title. This doubly confirms the F2.6 finding (HPO near-inert
on this data) and closes the question. (A 12-trial / capped-space / short-epoch search does not
*prove* no TCN anywhere beats 0.8194 — but with the ~0.82 ceiling and F2.6's null, the
engineering verdict is firm: representation was the right lever; *deep*, and *tuning the deep*,
were not.)

## ADR-008 — Governed model lifecycle: metric-gated promotion + rollback on MLflow **aliases** (not deprecated stages)

**Date:** 2026-07-01 · **Phase:** F3 · **Status:** accepted

**Context.** F2 registers the winning model *version* in the MLflow Model Registry, but
"which version is actually **in production**, and how does a new one get there (or get
undone)" was still folklore. F3 is the MLOps-spine gate the repo exists to close: a
**governed lifecycle** — a promotion that a *worse* model cannot pass, and a **rollback**
that restores the previously-live version — as tested, reproducible mechanism, not a
manual click in a UI. Two design questions fell out of building it on MLflow 3.

**Decision.**

1. **Production is a model-version *alias*, not a stage.** MLflow 3 **deprecated** the
   classic `Staging`/`Production` **stage** transitions (`transition_model_version_stage`)
   in favour of **model-version aliases**. So "production" here is an alias
   (`registry.PRODUCTION_ALIAS = "production"`) that points at exactly one version.
   Promotion re-points the alias; rollback re-points it back; serving (F4) will load
   `models:/<name>@production`. Building on the *current* API (aliases) rather than the
   deprecated one is the honest, forward-looking choice — and aliases are strictly more
   flexible (arbitrary named pointers, not a fixed four-stage enum).

2. **The gate reads each version's metric from its source run.** A candidate is compared
   against the incumbent production version on the primary metric
   (`config.PRIMARY_METRIC`, ROC-AUC), read from the run `train` logged it on before
   registering. It promotes only if `candidate >= incumbent - min_delta`. The default
   `min_delta = 0.0` means "at least as good"; a tie promotes (the newer version wins on
   equal evidence — a deliberate, documented choice), and a small `min_delta` can tolerate
   a negligible regression when a newer version is preferred for other reasons. A
   **first-ever** promotion (no incumbent) always passes — there is nothing to protect yet.

3. **A rejection is a governed *outcome*, not an error.** A worse candidate not promoting
   is the gate **working**, so `promote` returns a structured `PromotionResult`
   (`promoted=False` + the compared metrics + a human reason), not an exception. Only a
   malformed *request* — an unknown version, or a version whose run never logged the metric
   so the gate has nothing to compare — raises `PromotionError`. (The `pdm promote` CLI
   still exits non-zero on a rejection, so a script/CI step notices.)

4. **Rollback is deterministic via a superseding tag.** On a successful promotion the
   version it supersedes is written as a tag (`PREV_TAG`) on the newly-promoted version, so
   `rollback` restores the prior production version by reading that tag — no run-history
   scraping, fully offline-testable. A first-ever production version has no predecessor and
   `rollback` raises rather than guess.

**Why.** The questions a reviewer asks of a "production model" are exactly these: *can a
worse model reach production? how do you undo a bad promotion? where is that decision
recorded?* F3 answers each as a tested invariant — a gated alias move, a tag-based
rollback, a structured decision record — on the same server-free SQLite registry as F2.
The model is still deliberately cheap; the **governance around it** is the deliverable.

**Consequences.** New `registry.py` (`production_version`, `version_metric`,
`latest_version`, `promote`, `rollback`, `format_promotion`) + `pdm promote`
(`--version`/`--min-delta`/`--force`) and `pdm rollback`. `test_registry.py` (14, offline,
tmp SQLite): **a worse candidate does not promote** and **rollback restores the prior
version** (the DoD), plus first-promotion, tie, `min_delta` tolerance, `--force` bypass,
and the loud errors on malformed input. No new dependency (MLflow is already core). F4
serving loads the `production`-aliased version; F5's retrain loop ends in exactly this
gated `promote`.

---

## ADR-009 — Serving: a FastAPI app over the `production`-**aliased** model; probabilities via the native flavor; env-overridable backend for the container

**Date:** 2026-07-01 · **Phase:** F4 · **Status:** accepted

**Context.** F3 makes "which version is in production" a governed alias. F4 has to
*serve* that decision — turn readings into failure probabilities over HTTP — without
re-introducing the folklore F3 removed. The phase turns on one coupling and a couple of
smaller choices about *how* to load and *what* to return.

**Decision.**

1. **Serving resolves the model through the alias, not a pinned URI.** The app loads
   `models:/<name>@production` — the same `registry.PRODUCTION_ALIAS` that `promote` /
   `rollback` move. So a promotion or a rollback in F3 changes what `/predict` answers
   with **no redeploy and no config edit**; the alias is the contract between governance
   and serving. The load is **lazy and cached** (`ModelStore`): the app starts even with
   nothing promoted (`/health` → `model_loaded=false`), and clearing the cache re-resolves
   the alias — which is exactly what picking up a rollback needs.

2. **Probabilities via the native flavor, not the pyfunc default.** MLflow's generic
   `pyfunc` predict returns thresholded *labels* for the sklearn/LightGBM flavors we log,
   but the product is the failure **probability** (the positive-class column, the
   `models.Model.predict_proba` contract F2 trains and F3 gates against). So the model is
   loaded through its **native flavor** — read from the registered version's
   log-model-history tag, so a lightgbm winner and a logreg winner both serve correctly —
   and column 1 is returned. Serving what the gate measured keeps the whole pipeline
   coherent.

3. **era-NULL is a valid input, not a 422.** A missing signal (JSON `null`) is the
   era-NULL missingness ADR-003 preserves — a real, informative pattern the model handles
   (LightGBM natively, the LogReg pipeline via its imputer). `/predict` reindexes each
   request to the fixed `features.FEATURE_COLUMNS` order (so JSON key order can't scramble
   the frame), coerces to numeric with missing → NaN, and re-runs `assert_no_leakage` as a
   belt-and-braces guard. Healthy-but-not-ready is distinguished: `/health` returns 200
   even with no model; `/predict` and `/model-info` return **503** (not 500) when nothing
   is promoted.

4. **The backend is env-overridable so the container needs no code change.** A new
   `config.default_tracking_uri()` honours the standard `MLFLOW_TRACKING_URI` env var and
   otherwise falls back to the local `mlruns/` SQLite backend; `registry._client()` and the
   serving `ModelStore` both route through it. The Docker image points that var at a mounted
   registry volume — so the container serves the training host's registry with nothing baked
   in, and a promotion/rollback still flows through with no rebuild.

5. **The load touches NO global MLflow state — it resolves through the injected client to a
   local path.** The obvious `mlflow.<flavor>.load_model("models:/<name>@production")`
   resolves that URI through MLflow's **process-global** registry URI, and MLflow 3
   **pins** that URI once set — `set_registry_uri(None)` does *not* un-pin it (verified). So
   a serve load in one process would silently redirect a co-resident `train`'s
   `register_model` to the wrong backend — a real leak a two-model round-trip caught (the
   second model registered into the first's registry). Instead `ModelStore` resolves the
   `production` alias to a concrete `version` via the injected `MlflowClient`, asks that
   client for the version's artifact URI, `download_artifacts` normalises it (the raw URI is
   a Windows-hostile `file:C:/…` form) to a local path, and the flavor loader reads *that
   path* — no `models:/` resolution, no global URI mutation. The flavor itself is read from
   the model's own `MLmodel` metadata (`get_model_info().flavors`), **not** the
   `mlflow.log-model.history` run tag MLflow 3 no longer writes.

**Why.** The questions a reviewer asks of a served model are: *which governed version is
this? does it follow a rollback? is it the probability the gate measured?* F4 answers each
as tested behaviour — an alias-resolved load, a native-flavor probability, a 503 vs. 200
readiness split — on the same server-free SQLite backend, with the model still deliberately
cheap and the plumbing the deliverable.

**Consequences.** New `serve.py` (`create_app`, `ModelStore` with a client-resolved,
global-state-free load, `PredictRequest`/`Response`, `ModelInfo`) + `pdm serve --host/--port`
(uvicorn). `Dockerfile` (slim, `[serve]` extra, libgomp for LightGBM) + `docker-compose.yml`
(serving + the MLflow UI on one shared registry volume — you can *see* the version the alias
points at). `config.default_tracking_uri` (env-overridable) threads into `registry._client`.
The `[serve]` extra (fastapi/uvicorn/httpx) was already declared at F0. `test_serve.py` (10,
offline, tmp SQLite): the DoD **prediction round-trip** on a promoted fixture-trained model,
the health/model-info surface, the era-NULL passthrough + column reorder, the 503-without-a-model
paths, and a rollback picked up by a fresh store load — the multi-test run is what surfaced the
global-URI leak (decision 5), since only a second train-in-the-same-process exposes it. F5's
retrain loop will exercise exactly this serving surface after it promotes.

---

## ADR-010 — Characterize the ceiling: measure that 0.82 is the *data's* information limit — decomposition + a fenced label-leaking upper-bound + an OOF stacking redundancy probe

**Date:** 2026-07-01 (measured 2026-07-02) · **Phase:** F2.8 · **Status:** **accepted — MEASURED on the full data (GPU notebook). The thesis is REFUTED: the stacking probe beats its best base member (+0.0073 two-rung / +0.0063 three-rung), so the rungs are NOT fully redundant and "0.82 is purely the data's ceiling" is not confirmed. Kept honestly — see Measured outcome below.**

**Context.** Three sub-phases converged on the same claim: F2.6 (tuning is inert, +0.003),
F2.7 (temporal helps a little, +0.007; deep does not earn its place; tuning the deep does not
rescue it). Each *implies* the ≈0.82 ceiling is a property of the **data** (the generator's
ADR-020 degradation ramp is gentle, and most of the 168 h label window is genuinely
healthy-and-unpredictable), not a modelling shortfall — but every statement so far has been an
**assertion**. F2.8 is the capstone that turns the string of nulls into a **measured** thesis,
and then **stops the modelling investigation by design** (the repo's job is the MLOps spine,
F3→F6).

**Decision.** A new `ceiling.py` runs three instruments, all on the **exact F1 unit split /
seed / test rows** (apples-to-apples with F2/F2.7) and all **label-honest** — labels are read
here *only to grade or to bound*, never as an honest model input (the ADR-003 guard, the same
`detect_score.py` discipline):

1. **Score decomposition** (`decompose`). The honest per-row held-out AUC, sliced by
   **time-to-failure horizon** (right-open buckets `[0,6) … [168,∞) h`) and by **failure
   mode**. Each horizon/mode slice pits *that band's positives* against **all** held-out
   healthy rows, reading "how separable are failures this close / of this kind from healthy".
   This answers "is 0.82 flat, or high near failure and fading far out?" — the shape is the
   point, and the aggregate is a mixture over a mostly-unpredictable window by construction.
   Time-to-failure is derived **label-side** (`time_to_failure`: per unit, the failure event
   is one stride past the last positive row) and used **only** to bucket, never as a feature.

2. **A label-leaking upper-bound** (`upper_bound`) — a LightGBM that additionally sees
   `failure_mode` (one-hot) and the derived `time_to_failure_h`, bounding the **irreducible**
   error. It is a **diagnostic, clearly labelled, and structurally fenced**: it is built inside
   its own function, returned in its own field (never merged into a reported metric), and the
   honest frames are asserted leak-free by `_assert_honest_frame` (which adds a `LEAK_FEATURES`
   guard *on top of* `features.assert_no_leakage`, so even the non-target `time_to_failure_h`
   can never reach the honest path). A test asserts the fence fires. `gap = leaky − honest` is
   the most room any better model on the same honest features could ever have.

3. **A stacking redundancy probe** (`stacking_probe`) — an **out-of-fold, unit-grouped**
   (`GroupKFold`) `LogisticRegression` meta-learner over the base rungs' predictions. Fit on
   OOF train predictions (no base model's in-sample fit leaks into the meta score, no unit
   crosses a fold), evaluated on the shared test rows against base models refit on all train
   rows. **If the stack can't beat its best base member, the rungs are information-redundant →
   the ceiling is the data.** Reported either way — and *measured, it DID beat the best base*
   (see Measured outcome), so the rungs are **not** fully redundant. Stacking lives here **as a
   probe, not a product** — it is deliberately not promoted to a shipped model.

**The TCN seam (compute honesty).** Everything above is CPU-only on the low-end desktop (the
two base rungs are the cheap F2.7 LightGBM frames). The F2.7 **TCN** rung needs the GPU, so the
probe accepts its predictions through `extra_oof={name: (oof_train, proba_test)}` — a notebook
run folds the TCN's out-of-fold column in **without a rewrite**, alignment asserted. Because
F2.7 already measured the TCN *below* rung (b), the probe's **verdict** (redundant vs. not) is
already decidable from the two GBDT rungs; the TCN-included number is an optional refinement,
not a blocker. This is the same "tested path = CPU/fixture; reported number = GPU/full data"
discipline as ADR-007. *(Measured: the TCN OOF was in fact folded in — the three-rung verdict
matches the two-rung one.)*

**Measured outcome (2026-07-02, GPU notebook, seed 42, 3.47M rows).** Overall per-row honest
**0.8125**. Decomposition: sharp near failure — [0,6) h **0.9183**, [6,24) **0.9290**, [24,72)
**0.9264** — fading far out — [72,168) **0.7162**, [168,∞) 0.5555 (only 17 positives); by mode
bearing **0.8732** / oil_starve 0.7959 / overheat 0.7720. Fenced upper-bound 1.0000 → gap over
honest **+0.1875**. **Stacking probe: best base = lightgbm_temporal 0.8194; stack 0.8267
(+0.0073) two-rung, 0.8257 (+0.0063) three-rung with the TCN OOF folded in → `ceiling_is_data =
False` in both.** So the capstone **refutes** its own hypothesis: a modest but consistent
~+0.006–0.007 combinable signal remains between the per-row and temporal rungs (magnitude on par
with the +0.0069 F2.7 called "temporal helps", so it is not dismissible as noise; caveat: single
deterministic OOF meta-learner, no CI). The honest reading is *"the ≈0.82 aggregate is
data-dominated, but the rungs are not perfectly redundant — a little combinable signal is left,
and the instrument was built precisely so this could be discovered rather than assumed."* TCN
footnote: the same-geometry rung reproduces at **0.7979** here (torch 2.12+cu130) vs ADR-007's
0.8148 — a cross-CUDA-version numerics gap (deterministic *within* a build, not across; 8→12
epochs did not close it), and the TCN is the weakest rung so it does not change the verdict.

**Why.** The honest close of a "is the model the bottleneck?" investigation is a *measurement*
that the bottleneck is the data, not one more assertion. The decomposition shows **where**
predictability lives (a shape, not a scalar); the fenced upper-bound **bounds** what any model
could do on these features; the stacking probe **falsifiably tests** rung redundancy. Together
they convert the F2.5→F2.7 nulls into a proven thesis — and knowing to stop here (rather than
chase F2.9 RUL / F2.10 C-MAPSS, which are curated future work) is itself the senior signal.

**Consequences.** New `ceiling.py` (`time_to_failure`, `build_base`, `decompose`,
`upper_bound`, `stacking_probe`, `characterize`, `format_report`) + a `pdm ceiling` subcommand;
`test_ceiling.py` (13, offline/deterministic: ttf is label-side & exactly on positives, the
base frames are leak-free & on the exact F1 split, the decomposition covers horizon+modes, the
upper-bound bounds & is fenced — the fence fires — determinism, the stacking seam folds/rejects
extra OOF). No new dependency (LogisticRegression/GroupKFold are already in). **F2.8 closes the
F2.* modelling arc; the next build is F3 (registry + gated promotion, ADR-008) — the MLOps
spine the repo exists to finish.** The full-data decomposition/probe numbers were produced by a
`pdm ceiling` run on the GPU/full dataset (2026-07-02) and are recorded above + in STATE.

---

## ADR-013 — The drift → auto-retrain loop: a share-threshold drift trigger, and a Prefect flow that routes every promotion through the *same* F3 gate

**Date:** 2026-07-02 · **Phase:** F5 · **Status:** accepted

> **ADR number.** ADR-011 and ADR-012 are reserved for the deliberately-deferred F2.9 (RUL)
> and F2.10 (C-MAPSS) modelling sub-phases (scoped in ROADMAP, intentionally not built), so
> F5 takes the next free number, **ADR-013** — the roadmap already earmarked it for this phase.

**Context.** F2→F4 built the spine: train + track, register + gated-promote, serve the alias.
F5 is the capability the repo exists to demonstrate — the **closed loop** that ties them
together: incoming data drifts, a fresh model is trained on it, and it reaches production **only
if it earns it**, with no human in the path. Two decisions decide whether the loop is honest: how
drift becomes a *trigger*, and how a retrained model is allowed to *ship*.

**Decision.**

1. **Drift trigger = a share-of-features threshold, not "any feature drifted".** `monitor.py`
   runs Evidently's `DataDriftPreset` over exactly the model's input signals
   (`features.FEATURE_COLUMNS`, via `select_features` — the monitored surface is the trained
   surface, and the leakage guard rides along for free), then applies **our own** policy to the
   per-column drift flags: drift is declared when the **share** of drifted features reaches
   `config.DRIFT_SHARE_THRESHOLD` (0.5). A *share*, because a single column tripping on noise
   must not fire a retrain — the loop should react to a **distribution shift**, which the
   `season` stimulus produces across several correlated signals at once (a real heatwave moves
   the thermal cluster together). Evidently reports its own dataset-drift boolean at its default;
   we deliberately **ignore it** and decide under our single, documented threshold, so the retrain
   policy lives in one auditable place (`DriftReport.threshold` records it). The distilled
   `DriftReport` is small and JSON-serialisable; the raw Evidently object is returned separately
   for the HTML artifact.

2. **The retrain promotion is F3's gate, unchanged — so "auto-retrain" can never mean
   "auto-degrade".** `flows.py`'s promote task calls `registry.promote(...)` **verbatim**: a
   retrained candidate that does not clear the metric gate against the incumbent is **held**, and
   that is a normal, reported `FlowResult` outcome (`retrained=True, promoted=False`), not an
   error. This is the load-bearing honesty of the whole loop — the closed loop cannot silently
   ship a worse model, because the exact same governance that guards a manual `pdm promote` guards
   the automated one. A dedicated test proves it (`min_delta=-1.0` ⇒ impossible bar ⇒ the
   candidate is held and production stays the incumbent).

3. **Prefect authors/executes the flow; GitHub Actions only schedules it (they layer, ADR-002).**
   The loop is a Prefect `@flow` of retried `@task`s — `detect_drift → [if drift] → retrain →
   promote-or-hold` — and runs **in-process** on Prefect's default local runner, so the tests
   exercise the real task graph on the fixture with **no server** and the scheduled
   `retrain.yml` (already wired at F0) just invokes `pdm flow`. The Prefect decorators are
   imported **lazily inside** `run_drift_retrain` (not at module import), so importing the
   package — and core CI — never needs the `[ops]` extra; only actually running the loop does.

4. **Evidently is pinned `<0.7`.** 0.7.0 rewrote the public API (the `Report` + `metric_preset`
   + `as_dict()` surface `monitor.py` targets was replaced). Capping the `[ops]` extra keeps the
   code and the installed library in agreement across desktop and notebook — the same
   reproducibility discipline as the pinned generator (ADR-001).

**Why.** A drift→retrain loop is only a portfolio signal if it is *demonstrably safe*: the
reviewer's question is "what stops this from automatically promoting a worse model?" and the
answer is a tested `FlowResult` where the F3 gate held. Deciding drift on a share threshold (not
one noisy column, not Evidently's opaque default) makes the trigger legible; routing every
promotion through the unchanged gate makes the automation trustworthy; keeping it in-process and
`[ops]`-lazy keeps it fully offline-testable and core-CI-light.

**Consequences.** New `monitor.py` (`DriftReport`, `drift_report`, `detect_drift`,
`config.DRIFT_SHARE_THRESHOLD`) and `flows.py` (`FlowResult`, `run_drift_retrain` composing the
F5 monitor + F2 train + F3 promote); `pdm monitor` / `pdm flow` go live (the last two roadmap
stubs — `test_skeleton` now asserts the whole CLI surface is wired, no stub remains); the `[ops]`
extra is capped `evidently<0.7`. `test_monitor.py` (6) + `test_flows.py` (5), offline on the
fixture with a **synthetic** multi-signal shift standing in for the generator's `season`
stimulus, MLflow → tmp SQLite, Prefect in-process — both modules `importorskip` the `[ops]` libs
so core CI (which installs only `[dev]`) skips them cleanly, exactly like `[serve]`/`[tune]`/
`[deep]`. **F5 closes the marquee gate — a complete production spine now runs end to end:
train → registry → serve → drift → retrain → cloud-scheduled.** F6 (a hosted free-tier `/health`
link) is the only stretch left.

**Follow-up (2026-07-02) — the share threshold is set against the physics, not the feature
count; and the real `--season` regeneration path is fixed.** The first-ever real `[ops]` run
(these tests had always skipped for lack of the extra) surfaced two things:

1. **`DRIFT_SHARE_THRESHOLD` moved 0.5 → ⅓.** The original 0.5 was calibrated on the
   pre-`vibration_mms` **8-signal** surface, where the synthetic heatwave stand-in shifts 4
   correlated thermal signals = 4/8 = *exactly* the threshold. The 0.2.0 data refresh added
   `vibration_mms` (9 signals), so the *same* 4-signal shift became 4/9 = 0.44 and the trigger
   silently stopped firing (`drifted=False`, 4 drift tests red). The lesson is that pinning the
   policy to "half the columns" makes it an artifact of how many features happen to be monitored.
   Checking the generator, the real `heatwave`'s footprint is a **correlated cluster of ~3-4 of
   the 9 signals** — `ambient_delta_c=8.0` moves `coolant_temp_c` (`0.15·(ambient−25)`), and
   `wear_mult=1.20` moves the wear-coupled `coolant_temp_c` / `oil_pressure_kpa` / `vibration_mms`
   over accumulated wear; `egt_c` / `boost_pressure_kpa` are load/altitude-driven, not ambient.
   So the trigger should fire when *a cluster moves together* and still reject one or two noisy
   columns — that is **⅓**, chosen against the physics and independent of the exact feature count.
   The synthetic test keeps its 4-signal shift (4/9 = 0.44, a comfortable margin over ⅓ rather
   than sitting on the boundary). A clean "tests that never run hide arithmetic" finding.
2. **`data.regenerate_full(season=…)` now resolves the preset.** It stored the raw season
   *string* on the generator config via `replace(cfg, season="heatwave")`, but the generator's
   `ForgeConfig.season` is a `Season` **object** (ambient delta / wear / hazard multipliers), so
   `pdm monitor --season` / `pdm flow --season` (the real production path) raised
   `AttributeError: 'str' object has no attribute 'hazard_mult'` inside `config.validate()`. It
   now calls `can_telemetry_forge.config.resolve_season(season)` exactly as the generator's own
   `--season` CLI does. Only the injected-frame offline tests exercised `detect_drift` before, so
   the real-generator path had never run — the same "never-run code hides a defect" class as (1).

> **Known env seam (not F5 code):** Evidently 0.6.7's `as_dict()` trips a NumPy `np.histogram`
> `bincount` broadcast error on the **full** 3.47M-row regeneration under this Python 3.14 / NumPy
> build (the small fixture is fine, so the offline suite is unaffected). Measuring the real
> heatwave breadth end-to-end waits on an Evidently/NumPy bump; the ⅓ decision above is grounded
> in the generator physics, which does not depend on that run.

---

## ADR-014 — Hosted free-tier deploy: a self-contained image that **bakes a fixture-trained demo registry**, so a live `/health` shows a real served model without violating "never report off the fixture"

**Status.** Accepted (F6).

**Context.** F4 serves the `production`-aliased model over FastAPI; F6's goal is a **live,
clickable `/health` link** on a free tier (Hugging Face Spaces — a permanent free URL, no idle
cold-sleep, fits the ML narrative) so the serving layer is a reachable artifact, not just a
`docker compose up` claim. Two constraints collide:

1. **A fresh deploy has an empty registry.** The F4 image (`Dockerfile`) mounts an *external*
   registry volume — perfect for the compose stack, useless for a hosted deploy that has no
   volume to mount. With nothing promoted, `/health` honestly returns `model_loaded=false` and
   `/predict` 503s: a reviewer clicking the link sees no model. A weak showcase.
2. **ADR-001 forbids reporting a fixture-scored metric.** The free build runs offline (no
   generator, no network), so the only model it can bake is one trained on the committed *smoke
   fixture* — exactly the thing ADR-001 says never to report.

**Decision.** Ship a **separate self-contained image** (`Dockerfile.hf`) that **bakes a demo
registry at build time** via `scripts/seed_demo_registry.py`: train on the fixture → register the
winner → promote it to `production` through the **same F3 gate**. The endpoint then answers a real
prediction the moment it boots. The ADR-001 boundary is respected by **scope, not by hiding it**:
ADR-001 forbids *reporting* a fixture-scored number, not *serving* a demo to prove the endpoint is
wired. The demo status is made **unmissable on every surface** — the registered version is tagged
`demo=fixture`, `/model-info` exposes the metric so the (fixture) provenance is inspectable, and
the README's live-link line + `docs/DEPLOY.md` both call it a demo model. The real ≈0.82 model is
the one `pdm train` produces on the full data locally, and that is the only number ever quoted.

**Two load-bearing details.**

- **A class-rich fixture seed (0), not the default (42).** The fixture's unit-grouped split at
  the default seed 42 lands a **single-class** held-out set → `DegenerateSplit` (an ADR-004
  fixture artifact; the full data is class-rich at any seed). The bake pins seed **0**, which is
  class-rich, so the fixture train scores a real ROC-AUC and registers a usable model. (Verified:
  seeds 0/1/2/4/6/8/9/10/11 are class-rich on this fixture; 42 is not.)
- **Build path must equal run path.** MLflow stores artifact locations as **absolute paths** in
  the tracking DB, so a baked store is only relocatable if the path is unchanged. `Dockerfile.hf`
  fixes it at `/mlflow` for both bake and serve, and the seed script pins an explicit experiment
  `artifact_location` **inside** the store dir (MLflow otherwise scatters artifacts to a
  CWD-relative `mlruns/` that would not travel with the image) — so the whole store is one
  self-contained tree the image carries.

**Also in F6.** The scheduled `retrain.yml` workflow, a marked F5 *placeholder* until now, is
wired to run `pdm flow` for real (installs `[ops,generate]`, since the loop regenerates the
season-shifted window via the pinned generator). It uses the **real F3 gate** (no `--min-delta`
escape), so the cloud-scheduled loop cannot auto-degrade either — the same guarantee as F5's
in-process loop, now demonstrated executing on free cloud infra.

**Alternatives rejected.**
- *Deploy empty (`model_loaded=false`).* The purest live link, but a reviewer clicking it sees no
  model — weaker as a showcase, and the honest-status can be preserved just as well by labelling
  the demo, which we do.
- *Bake a full-data model.* Impossible offline on a free runner (no generator/network) and it
  would drag the multi-GB dataset into the build; also pointless, since the served demo's *number*
  is never reported regardless.
- *Reuse the F4 `Dockerfile`.* Its mounted-volume design has no promoted model on a hosted deploy;
  a self-contained image is the right tool for a host with no persistent volume.

**Consequences.** New `Dockerfile.hf` (self-contained, non-root UID 1000, HF `app_port` 8000) +
`scripts/seed_demo_registry.py` (`seed_registry` + a `--store-dir` CLI) + `docs/DEPLOY.md` (the HF
Spaces front-matter, the push-to-Space steps, the local build smoke-test, Render/Fly.io
alternatives on the same image). `test_seed_demo_registry.py` (4, offline, `[serve]`-gated):
promotes a **demo-tagged** version, a **fresh** serving process over the baked store reads
`model_loaded=true` and predicts, the store is **self-contained** (artifacts colocated), and the
bake is **deterministic** (same seed → same probabilities). `retrain.yml` runs `pdm flow`.
**The production spine is now not just complete but reachable — a live `/health` a reviewer can
click.** The bake was verified end-to-end natively (a fresh `ModelStore` over the baked store
serves a real `/predict`); the container build runs on HF's runners.

## ADR-015 — Managed-cloud deploy: the same image on **Cloud Run** + a **Cloud SQL (Postgres)** prediction log behind an interactive demo, so "operate managed cloud in production" is demonstrated, not just "a container that builds"

**Status.** Accepted (F7) — **superseded on the resource choice by ADR-016**: Cloud SQL has
no free tier, so the shipped deploy uses **Neon** (free serverless Postgres) as the managed
resource. Everything below stands except "Cloud SQL" → "a managed Postgres (Neon)". **LIVE
2026-07-04** at https://forge-pdm-mlops-958199756179.us-central1.run.app.

**Context.** F6 put a live `/health` on Hugging Face Spaces — but Spaces is *free-tier
hosting*, which is deliberately **not** the same claim as operating a **managed cloud
runtime + a managed resource in production**. That distinction is a real hiring gate:
"containerize an app" (F4/F6, done) is junior-table-stakes; "deploy and operate it on a
managed cloud platform with a managed database" is the senior signal that "an image that
builds" explicitly does **not** cover. F7 closes exactly that gate — and no other. Two
sub-problems:

1. **Which managed runtime.** A raw VM (e.g. an Oracle/GCE free instance) proves *IaaS* —
   "I ran a box" — which is closer to self-hosting, the very thing the gate discounts. A
   *managed container platform* is the direct hit.
2. **What gives a managed database an honest job.** The F4 serving layer is
   (correctly) stateless — resolve the model, answer a probability — so a managed DB
   bolted onto it would be decorative. Without real state to persist, the "managed
   resource" half of the gate is unconvincing.

**Decision.** Deploy the **same self-contained `Dockerfile.hf` image** (already
`$PORT`-aware via `scripts/hf_entrypoint.sh`, which bakes the demo registry at startup) to
**Google Cloud Run** — a *managed, serverless container runtime* (scale-to-zero, revisions,
managed TLS), not a VM — and pair it with **Cloud SQL for Postgres** as a **managed
relational resource**. The database earns its keep through a new **interactive demo UI**
(`GET /demo` + `POST /demo/predict`): a "set the J1939 parameters → get the failure
probability" page (the click-and-try pattern the receivables-agent showcase already uses),
and every served demo prediction is **logged to Cloud SQL** and read back into a "recent
predictions" panel. So the managed resource has a real, visible production job — that is
what makes it *managed cloud in production* rather than *a managed container*.

**Graceful degrade is a hard invariant, not a nicety.** `store_pg.open_log()` returns
`None` whenever `DATABASE_URL` is unset — which is **every non-cloud target**: a local
`pdm serve`, the F6 HF Space, and CI. The demo then runs *without* persistence (the
prediction still returns; the recent panel is just empty and the page says so). This means
adding the managed resource **cannot break any existing deploy**, and the offline tests
never need a server — the exact same `store_pg` code runs against a tmp **SQLite** file in
tests and against Cloud SQL in production (SQLAlchemy Core over both). A logging failure is
also swallowed to a no-op: the model already answered, so a transient DB blip never turns a
successful prediction into a 500.

**The honesty boundary (ADR-001 / ADR-014) is unchanged.** The served model is still the
fixture-trained **demo** — the `/demo` page carries the same `demo=fixture` banner as
`/model-info` and the README, and the only reported number remains the ≈0.82 full-data
local model. **No PII, by construction:** the log stores only the submitted J1939 signal
values (synthetic sensor floats, restricted to `features.FEATURE_COLUMNS` so a crafted
request key can't widen the row), the returned probability, the model version, and a UTC
timestamp — there is no user identity anywhere in the schema.

**No secrets committed.** `scripts/deploy_cloudrun.sh` generates the DB password at deploy
time, stores the `DATABASE_URL` in **Secret Manager**, and injects it into the service via
`--set-secrets`; Cloud Run reaches Cloud SQL over the `/cloudsql/<connection>` unix socket
(`--add-cloudsql-instances`). The repo carries only the parametrized script — no credential.

**Alternatives rejected.**
- *An Oracle / GCE free VM.* Generous free compute, but it earns the *IaaS* badge, not the
  *managed* one the gate is about — a VM you patch and babysit reads as self-hosting.
- *Firestore (serverless NoSQL) as the resource.* Genuinely managed and scale-to-zero, but
  a document store reads as a weaker "production database" claim than managed Postgres; the
  prediction log is naturally relational, so Cloud SQL is the honest fit.
- *A new Cloud-Run-specific Dockerfile.* Unnecessary — `Dockerfile.hf` already honours
  `$PORT` and bakes the demo at startup, so Cloud Run reuses it as-is. One image, one
  honest artifact, two hosts (HF + Cloud Run), differing only in platform glue.
- *Make the demo stateless (skip the DB).* Then the managed-resource half of the gate has
  nothing to demonstrate — the DB is the point, not an add-on.

**Consequences.** New `src/pdm_mlops/store_pg.py` (the append/read log, graceful-degrade,
lazy `[cloud]` import) + the `[cloud]` extra (`sqlalchemy` + `psycopg`); `serve.py` gains
`/demo` (a self-contained inline-CSS/JS page — no CDN, clean-room-safe), `/demo/predict`
(scores + logs), and an injectable `prediction_log` on `create_app` (defaulting to
`open_log()`); `scripts/deploy_cloudrun.sh` (Artifact Registry + Cloud Build + Cloud SQL +
Secret Manager + `gcloud run deploy`); `docs/DEPLOY.md` gains a "Managed cloud: Cloud Run +
Cloud SQL" section (with a tear-down). Tests: `test_store_pg.py` (8, `[cloud]`-gated:
round-trip, no-PII key restriction, era-NULL, graceful-degrade on unset/bad URL, best-effort
on a backend error) + `test_demo.py` (`[serve]`- and `[cloud]`-gated: demo round-trip with
and without a log, the 503 contract, the honesty banner on the page, persistence + the
recent-predictions panel). **This closes the managed-cloud gate the F0–F6 spine deliberately
left open** — the same governed model, now served from a managed runtime with a managed
resource behind it, at a clickable URL.


## ADR-016 — Close the managed-cloud gate at **$0**: keep Cloud Run, swap the managed resource to **Neon** (free serverless Postgres); ship the image with `.[serve,cloud]`

**Status.** Accepted (F7). **LIVE 2026-07-04** at
https://forge-pdm-mlops-958199756179.us-central1.run.app (revision 00003) — `/health` →
`model_loaded:true`; `POST /demo/predict` → `persisted:true`; `/demo` reads the rows back.

**Context.** ADR-015 specified **Cloud SQL** as the managed resource. Taking it live surfaced
the blocker: **Cloud SQL has no free tier** — the smallest `db-f1-micro` bills ~$8-10/mo for
as long as the instance exists. This is a **portfolio showcase with no budget**; a metered
resource with a tear-down clock is the wrong shape for a "click the live link" artifact.
Cloud Run itself is genuinely free (perpetual free tier, scale-to-zero → $0 idle); the *only*
thing breaking $0 was the database. The gate objective — *a managed runtime + a managed
resource in production* — does not require the resource to be GCP-native.

**Decision.** Keep **Cloud Run** as the managed runtime; make the managed resource **Neon**
(neon.tech) — a **serverless Postgres with a real free tier** (scale-to-zero, no card). The
gate is fully closed (managed runtime + managed resource, both live), at **$0/mo**, with no
tear-down clock — and it is arguably a *stronger* modern signal than a `db-f1-micro`. Because
`store_pg.open_log(url)` takes **any** SQLAlchemy URL, this is a **zero-application-code
change**: only the `DATABASE_URL` secret differs (a Neon `postgresql://…?sslmode=require`
string, rewritten to the `postgresql+psycopg://` dialect and stored in Secret Manager exactly
as before). New `scripts/deploy_cloudrun_neon.sh` = the ADR-015 flow minus Cloud SQL creation
and the `/cloudsql` unix socket (Neon is reached over the public internet with TLS; Cloud Run
egress is free). The original `deploy_cloudrun.sh` is retained as the **paid Cloud SQL
alternative**.

**Two deploy bugs found + fixed live — both reusable lessons.**
1. **A missing extra hid behind graceful degrade.** `Dockerfile.hf` installed `.[serve]`, not
   `.[serve,cloud]`, so the **`psycopg` driver was absent**. `create_engine("postgresql+psycopg://…")`
   raised, `open_log()` swallowed it (by design — a bad/unreachable DB must not break app
   start), returned `None`, and the demo reported `persisted:false` while the endpoint kept
   working perfectly. **The graceful-degrade invariant that makes the resource safe to add is
   the same property that masks a missing dependency** — so "the app is up and answering" is
   *not* evidence the managed resource is wired; only `persisted:true` + a read-back is. Fix:
   build with `.[serve,cloud]` (the driver is dormant on HF, where `DATABASE_URL` is unset).
2. **Hard-coded backend name violated the honest-output rule.** The `/demo` page label and the
   `serve.py`/`store_pg.py` docstrings said "Cloud SQL", but the deploy is Neon. Corrected to
   "managed Postgres (Neon)". Backend-specific copy is a honesty-drift hazard when the same
   image can point at more than one resource.

**Alternatives rejected.**
- *A GCP-native free managed SQL.* None exists — Cloud SQL and AlloyDB are both paid, no
  always-free micro. Firestore is free but NoSQL (ADR-015 already rejected it as a weaker
  "production database" claim than Postgres).
- *Render/Fly.io instead of Cloud Run (no card at all).* Fully free and cardless, but loses
  the recognizable **Google Cloud Run** signal; Cloud Run's free tier + a card on file (with
  scale-to-zero + no Cloud SQL, the actual bill is $0) is the better resume hit. Kept as a
  documented fallback in `docs/DEPLOY.md`.
- *Supabase instead of Neon.* Also free managed Postgres, but its free project pauses after a
  week of inactivity; Neon's scale-to-zero resumes on connect, better for an always-clickable
  demo.

**Consequences.** `Dockerfile.hf` installs `.[serve,cloud]`; new `scripts/deploy_cloudrun_neon.sh`;
`serve.py` + `store_pg.py` labels/docstrings say "Neon Postgres"; `docs/DEPLOY.md` leads with the
Neon-free path (Cloud SQL demoted to the paid alternative); `README` gains the F7 badge, the live
`/demo` link, and F0–F7 status; `.gcloudignore` keeps the Cloud Build upload lean. The gate is
**closed and live at $0**. *(No test change: the offline `[cloud]` tests already run against tmp
SQLite via the same `store_pg` code path, backend-agnostic by construction.)*
