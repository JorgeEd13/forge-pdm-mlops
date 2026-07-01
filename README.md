<p align="center">
  <img src="assets/logo.png" alt="forge-pdm-mlops" width="440">
</p>

<h1 align="center">forge-pdm-mlops</h1>

<p align="center"><em>An MLOps pipeline over synthetic predictive-maintenance telemetry — train, track, register, serve, and a drift → auto-retrain loop you can watch close.</em></p>

<p align="center">
  <img src="https://img.shields.io/badge/status-F2.8%20%E2%80%94%20modelling%20arc%20closed-yellow" alt="Status: F2.8 — modelling arc closed">
  <img src="https://img.shields.io/badge/ROC--AUC-~0.82-success" alt="ROC-AUC ~0.82">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/tracking-MLflow-0194E2" alt="MLflow">
  <img src="https://img.shields.io/badge/serving-FastAPI-009688" alt="FastAPI">
  <img src="https://img.shields.io/badge/orchestration-Prefect-070E10" alt="Prefect">
  <img src="https://img.shields.io/badge/data-100%25%20synthetic-blueviolet" alt="100% synthetic data">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License"></a>
</p>

The **MLOps half** of a two-repo story. Its companion
[`can-telemetry-forge`](../can-telemetry-forge) is a clean-room generator of
synthetic, **SAE J1939-grounded** heavy-equipment telemetry; this repo is the
**production ML system on top of it**: it trains a failure classifier, tracks every
experiment and registers models with **MLflow**, serves the promoted model with
**FastAPI**, and closes a **drift → auto-retrain loop** with **Evidently** +
**Prefect** — scheduled on free cloud runners by **GitHub Actions**.

> *Built the data engine, then the ML-in-production system over it.*

## The marquee — a closed drift → retrain loop

The generator exposes a `season` knob that shifts the whole fleet's operating
distribution (a `heatwave` runs the machines hotter). That is the drift stimulus:

```
baseline model  ──serve──►  production
        │
   --season heatwave  ──►  the data distribution drifts
        │
   drift monitor FIRES (Evidently)  ──►  retrain on the new distribution
        │
   re-run the SAME model comparison  ──►  register + promote the recovered model
```

Nothing about the model is clever — that's the point. The dataset is *diverse,
statistically credible, and fully reproducible*, so the **pipeline around it**
(tracking, registry, serving, drift, orchestration) is the thing on display.

> ⚠️ **Honest status — F2.8 (the modelling arc is closed).** F0 (skeleton), F1 (real data layer +
> leakage-safe features), **F2 (the training core — a two-model comparison, winner
> registered in MLflow)**, **F2.5 (outlier robustness — a ground-truth-scored detection
> ladder → a leakage-safe `signal_suspect` feature)**, **F2.6 (grouped-CV Optuna HPO
> + model diagnostics + training watchers)**, **F2.7 (a temporal-modelling ladder — does
> the trajectory help?)** and **F2.8 (characterize the ceiling — is the limit the model or the
> data?)** are in place, closing the F2.\* modelling arc by design. `serve`, the registry
> promotion gate, and the drift loop land across F3–F5 — see [`docs/ROADMAP.md`](docs/ROADMAP.md).
> Nothing here implies a live production deployment; the drift→retrain loop, once shipped, is a
> **demonstrated closed loop on synthetic data**.
>
> 🔎 **The score is real (≈ 0.82), and that took fixing the *data*, not the model.**
> Early on the classifier scored ≈ 0.55 — chance. Rather than tune the model, I measured
> *why*: a failing unit's pre-failure rows were statistically identical to its healthy
> rows, so there was **no signal to learn**. The root cause was upstream, in the
> generator — failures had a *when* but the sensors had no *path toward* it. Fixed there
> (`can-telemetry-forge` v0.2.0 added **progressive pre-failure degradation**), with the
> leakage guards intact, the same model reaches **≈ 0.82 ROC-AUC** (LightGBM test
> **0.8152**). Two attempts to also rebalance the failure hazard were measured and
> **rejected** (neither beat the fix).
> *Finding that my own showcase was measuring at chance — and saying so — is the point.*
>
> 🔬 **And the hyper-parameter search? Measured — it doesn't move the number.** Running the
> full grouped-CV Optuna study end-to-end (baseline vs tuned on the *same* cleaned frame,
> only the hyper-parameters differ) lifts the held-out AUC by **+0.0034 (LightGBM
> 0.8118→0.8152) / +0.0000 (LogReg 0.7131→0.7131)** — essentially nothing. The lift came
> from the **data**, not the tuning. That near-zero delta is the honest, deliberate result:
> HPO here is **instrumentation** (a tracked, leakage-safe, self-deception-proof search),
> not an accuracy trick (ADR-006). Both models also pass the `--audit` watchers on the full
> 134-unit data — the overfit trip is a 15-unit *fixture* artifact, not a model bug.

## Why the data is trustworthy (and reproducible)

- **100% synthetic, clean-room.** All data comes from the public J1939-grounded
  generator. No proprietary code or data, ever.
- **One dataset across every machine.** [`configs/dataset.json`](configs/dataset.json)
  is the single source of truth; with the **pinned** generator version, any machine
  regenerates **byte-identical** data. No "different dataset on the laptop."
- **The committed sample is a smoke fixture, not a training set.** Reported models
  always train on the **full** dataset regenerated from the canonical config;
  `data/sample_readings.parquet` exists only so `clone && pytest` and CI run offline
  (see [ADR-001](docs/DECISIONS.md)).

## Honest evaluation, baked in (F1)

A predictive-maintenance score is easy to inflate by accident. The feature layer
([`features.py`](src/pdm_mlops/features.py)) makes three guards **tested invariants**,
not good intentions:

- **A leakage guard that fails the build.** Inputs are the J1939 **sensor signals
  only**. The target and its label-side bookkeeping (`failure_mode`, `anomaly_type`,
  `is_outlier`) are knowable only *because* the failure already happened, so
  `assert_no_leakage` **raises** if any of them reaches the feature matrix — and a
  test asserts it actually fires.
- **Era-gated missingness is kept as signal, not imputed.** Older machines never had
  some sensors, so whole units are `NULL` for those channels. That missingness is
  informative, so the feature frame preserves it (LightGBM consumes `NaN` natively;
  the LogReg pipeline imputes at *model* time, in F2) — no blind imputation upstream.
- **The train/test split is by *unit*.** Each machine's readings are an autocorrelated
  time series, so a random row split would leak one unit's behaviour across the
  boundary and inflate the score. A seeded `GroupShuffleSplit` keeps every unit on one
  side only; disjointness is asserted, not assumed.

Determinism threads end to end — one seed → data → split → metrics. Rationale in
[ADR-003](docs/DECISIONS.md).

## Model selection as an MLOps process (F2)

`pdm train` doesn't fit *a* model — it runs an **honest comparison** and leaves a
tracked trail of it ([`train.py`](src/pdm_mlops/train.py)):

- **Two contenders, one interface.** A scikit-learn `LogisticRegression` pipeline
  (median-impute → scale → logreg) and a `LightGBM` classifier, behind one
  `fit`/`predict_proba` ([`models.py`](src/pdm_mlops/models.py)). Imputation lives **in
  the LogReg pipeline**, so the era-NULL missingness stays intact upstream and only the
  model that needs it fills it; LightGBM sees the `NaN` natively.
- **Both tracked, the winner registered.** Each model is an **MLflow run** (params +
  ROC-AUC + the fitted artifact); the best `roc_auc` is **registered** in the MLflow
  Model Registry. "Which model is current, and on what evidence" is recorded, not
  folklore — F3 builds gated promotion on this.
- **Local, server-free, deterministic.** Tracking + registry run on a local **SQLite**
  backend (no daemon, no paid service); same seed → same metrics. A `DegenerateSplit`
  guard fails loudly rather than logging a meaningless `nan` if a tiny-fixture split
  lands single-class. Rationale in [ADR-004](docs/DECISIONS.md).

```bash
pdm train                 # full dataset (needs the [generate] extra); SQLite-tracked
pdm train --no-register   # compare + track without touching the registry
```

## Outlier robustness, scored against ground truth (F2.5)

The generator injects nine defect families on purpose — some **obvious** (a signal out
of range), most **subtle** (a `joint_outlier` where each signal is plausible but the
*combination* isn't; a `sensor_stuck` that freezes in range; a slow `sensor_drift`). A
serious PdM pipeline has to survive these, and cleaning belongs *before* tuning. So
F2.5 is a **detection ladder, every rung scored against the generator's labels**
([`detect.py`](src/pdm_mlops/detect.py), [`detect_score.py`](src/pdm_mlops/detect_score.py)):

- **Multivariate** — `IsolationForest` + a robust-covariance **Mahalanobis** distance,
  for the joint outlier a per-column check misses.
- **Temporal** — `sensor_stuck` / `sensor_drift`. The first rolling-variance/slope
  version **scored ~0.02 F1 and flagged 85% of rows** — useless. The diagnosis was the
  fix: a stuck sensor isn't "low variance" (a running engine's other signals drown it),
  it's **one signal repeating its exact value**; the rewrite (with the detectable
  signals chosen *unsupervised* at fit time) reaches ~0.59 stuck recall at ~0.64
  precision. The negative result is documented, not hidden ([ADR-005](docs/DECISIONS.md)).
- **Autoencoder** — a small CPU-only PyTorch AE (optional `[deep]` extra, kept out of
  core CI). It has to **earn its place**, scored head-to-head with the cheap rungs — and
  it does (best overall, beats them on subtle recall).

**The honesty rule:** detectors run on the **signals only**; the `is_outlier` /
`anomaly_type` labels are read in exactly one place — the scoring harness — to *grade*
the detectors and *tune* thresholds, **never** as a detector input or a model feature
(the F1 leakage guard stays sacred, asserted by test). The output is a **leakage-safe
`signal_suspect` feature** ([`suspect.py`](src/pdm_mlops/suspect.py)) the downstream
model can use, plus a **data-quality watcher** that fails loud when a batch's outlier
rate spikes (it doubles as an F5 drift signal).

```bash
pdm detect                # run the ladder on the full dataset, print the scored table
pdm detect --autoencoder  # include the [deep] torch rung
```

## Tuning as a tracked, honest process (F2.6)

On the **cleaned** F2.5 inputs, F2.6 makes "why this model, with these params"
*visible and guarded* — deliberately **not** an accuracy play
([`tune.py`](src/pdm_mlops/tune.py), [`diagnostics.py`](src/pdm_mlops/diagnostics.py)):

- **HPO that can't cheat.** An **Optuna** study per model, scored by **unit-grouped
  cross-validation** (`GroupKFold`) — so the search can't leak a unit across folds — on
  the *training* split only, so it never tunes against the reported test number. Each
  study is tracked to MLflow; tuned params feed `train`.
- **Diagnostics as artifacts, not scalars.** Per fitted model: feature importance,
  calibration, a precision/recall threshold sweep, a learning curve — written as CSVs
  (always) + PNGs (when matplotlib is present).
- **Training watchers that fail loud.** An **overfit-gap** guard (train − CV AUC) and a
  **majority-baseline** guard (must beat 0.5), opt-in via `--audit`. On the tiny smoke
  fixture the deep model genuinely overfits and the guard **trips** — kept and tested as
  *the guard working*, not silenced ([ADR-006](docs/DECISIONS.md)).

**Measured, not asserted.** Run end-to-end on the full dataset, the search moves the
held-out AUC by **+0.0034 (LightGBM) / +0.0000 (LogReg)** — so "not an accuracy play" is a
*number*, not a hedge: the real ≈0.82 came from the data (ADR-020), not the tuning. Both
models pass `--audit` on the full 134-unit data (the overfit trip is a fixture-size
artifact). The honest deliverable here is the visible, leakage-safe process — and a
near-zero delta reported plainly.

```bash
pdm tune                          # grouped-CV Optuna HPO, tracked to MLflow
pdm train --tune --audit --diagnose   # tune, then train on tuned params with guards + artifacts
```

## Why F2 keeps sub-phasing — one question, measured to exhaustion

The string of `F2.x` phases isn't scope creep — it's **one question pursued honestly**: *how
good can this model legitimately get, and what is actually limiting it?* Each sub-phase is the
next logical probe, and **every answer (including the negative ones) is the deliverable**:

| Sub-phase | Probe | Measured answer |
|---|---|---|
| **F2.5** | Are dirty inputs the limit? *(clean first)* | A scored detection ladder → a leakage-safe `signal_suspect` feature. |
| **F2.6** | Is it the **hyper-parameters**? | **No** — HPO moves held-out AUC **+0.003 / +0.000**. The lift was the *data*, not tuning. |
| **F2.7** | Is it the **representation** (per-row throws away the trajectory)? | **A little, and not the deep model.** per-row **0.8125** → temporal-features LightGBM **0.8194** (+0.007, temporal *does* help) → causal **TCN 0.8148** (−0.005, doesn't earn its place). Tuning the TCN (grouped-CV HPO) → **0.8107**, still below. The cheap, interpretable model wins. |
| **F2.8** | Is the ceiling the **model or the data**? *(the capstone — prove it)* | **The data.** Three converging probes: AUC **decomposed** by horizon/failure-mode (predictability lives near the event; the rest is healthy-and-unpredictable by construction), a fenced **label-leaking upper bound** (the oracle barely clears the honest model → little is recoverable), and an **OOF stacking redundancy probe** (a meta-learner can't beat its best base model → the models are information-redundant). Not asserted — measured. |

The through-line: **the score is an *information* ceiling (~0.82), set by the data, not a
modelling ceiling.** A senior result isn't a bigger number squeezed out by force — it's
*knowing where the number comes from and saying so*. (If a 12-trial HPO had reliably beaten the
baseline on ceiling-limited synthetic data, **that** would be the red flag.)

The F2.* arc closes with **one capstone**, then deliberately stops (see
[`docs/ROADMAP.md`](docs/ROADMAP.md)) — the engineering attitude is to *characterize the wall*,
**never** to torture the number upward, and to **know when to stop**:

- **F2.8 — characterize the ceiling** *(done — the capstone)*. *Measured* (not asserted) that 0.82 is
  the data's limit: a per-horizon / per-failure-mode decomposition, a fenced-off label-leaking upper
  bound (a diagnostic, never a reported metric — asserted by test), and an out-of-fold stacking
  redundancy probe. All three converge — the ceiling is the **data**, not the model. This turns the
  F2.5→2.7 string of measured nulls into a *proven thesis*, and **the modelling investigation stops
  here, by design.** *(The reported full-data numbers come from a GPU `pdm ceiling` run; the tiny
  offline fixture is smoke only.)*
- **F2.9 (RUL / graded label) & F2.10 (NASA C-MAPSS) — scoped, deferred by design.** The next
  rigorous steps *are* identified — reframe the binary target to remaining-useful-life (where the
  trajectory becomes separable), and cross-validate on the canonical public benchmark where
  temporal models win. But the rigor is already proven, and this repo's job is the **production
  spine** (F3+), not a deeper modelling branch. They live as **curated future work** — a
  deep-learning axis better owned by a dedicated showcase. *Choosing the spine over more
  sub-phases is the call on record.*

```bash
pdm sequence                      # F2.7 — the three-rung temporal ladder, same split / test rows
pdm sequence --epochs 12 --register   # full TCN run on the GPU; register the winning rung
pdm ceiling                       # F2.8 — decomposition + fenced upper-bound + stacking redundancy probe
```

## The stack (and why two orchestration layers)

| Concern | Tool | Note |
|---------|------|------|
| Experiment tracking + model registry | **MLflow** | Local **SQLite** backend — no server, no cost. |
| Model | **scikit-learn** + **LightGBM** | A LogReg baseline and a LightGBM contender, compared *through MLflow* — model selection as an MLOps process. |
| Serving | **FastAPI** | Serves the promoted registry model. |
| Drift monitoring | **Evidently** | Baseline vs. a `season`-shifted distribution. |
| Orchestration (the DAG) | **Prefect** | Authors `detect → retrain → evaluate → promote` with retries; runs in-process for tests. |
| Scheduled cloud execution | **GitHub Actions** | Triggers the Prefect flow on a cron, on free runners. |

Prefect and GitHub Actions sit at **different layers** — Actions is the
*scheduler/trigger*, Prefect is the *flow author/executor* — and each closes a
distinct gap: named orchestration (Prefect) and free-tier cloud execution (Actions).
Full rationale in [ADR-002](docs/DECISIONS.md).

## Quickstart

```bash
# Clone & run offline (uses the committed smoke fixture, no generator needed):
pip install -e .[dev]
pytest -q
pdm --version

# Real runs regenerate the FULL dataset from the canonical config (F1+).
# Install the pinned generator to enable it:
pip install -e .[generate]
```

The `pdm` CLI surface (subcommands fill in by phase):

```bash
pdm train             # F2   — train both models, track to MLflow, register the winner (LIVE)
pdm detect            # F2.5 — run the outlier-detection ladder, scored vs. ground truth (LIVE)
pdm tune              # F2.6 — grouped-CV Optuna HPO on the cleaned inputs, tracked (LIVE)
pdm sequence          # F2.7 — three-rung temporal ladder (per-row / temporal / causal TCN) (LIVE)
pdm serve             # F4   — FastAPI serving the promoted model
pdm flow --season heatwave   # F5 — the drift → retrain loop (the marquee)
pdm monitor           # F5   — an Evidently drift report
```

## Roadmap

| Phase | What |
|------|------|
| **F0** | Foundations & runnable skeleton (package, CLI, CI, canonical config, smoke fixture) ✅ |
| **F1** | Data adapter (full regeneration + offline fallback) + leakage-safe features ✅ |
| **F2** | Train + track — two models to MLflow, the winner registered (**MVP core**) ✅ |
| **F2.5** | **Outlier robustness (clean first)** — multivariate + temporal + autoencoder anomaly detection on signals, scored vs. ground-truth labels → a leakage-safe `signal_suspect` feature ✅ |
| **F2.6** | Tune + diagnose — grouped-CV Optuna HPO on the cleaned inputs + model diagnostics + training watchers ✅ |
| **F2.7** | **Temporal modelling** — a three-rung ladder (per-row → temporal-features → causal **TCN**); temporal helps a little, the deep model doesn't earn its place (measured, reported either way) ✅ |
| **F2.8** | *(next)* Characterize the ceiling — horizon/mode AUC decomposition + leaky upper-bound + stacking redundancy probe; the capstone that closes the F2.* arc ☐ |
| **F2.9** | ↗ *future work (deferred by design)* — task reframing to RUL / graded label |
| **F2.10** | ↗ *future work (deferred by design)* — cross-dataset validation on **NASA C-MAPSS** |
| **F3** | Model registry — gated stage→production promotion + rollback ☐ |
| **F4** | Serving — FastAPI + Dockerfile + compose (serving + MLflow UI) ☐ |
| **F5** | **Drift monitoring + the auto-retrain loop (marquee)** ☐ |
| **F6** | *(stretch)* hosted free-tier deploy → a live `/health` link ☐ |

See [`docs/ROADMAP.md`](docs/ROADMAP.md) for objectives and definitions of done, and
[`docs/DECISIONS.md`](docs/DECISIONS.md) for the design rationale (ADRs).

## Project context

A public, clean-room portfolio project, and the downstream half of a pair: it
consumes [`can-telemetry-forge`](../can-telemetry-forge) as its data source
(experiment tracking, model registry, serving, and drift monitoring on the
telemetry produced there).

## License

[MIT](LICENSE) © 2026 Jorge Ribeiro
