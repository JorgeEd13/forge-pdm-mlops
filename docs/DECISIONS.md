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
   `can-telemetry-forge==0.1.0`). "Same config" only reproduces if the generator
   version matches too, so the version is pinned, not floating.
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
