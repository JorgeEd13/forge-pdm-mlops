<p align="center">
  <img src="assets/logo.png" alt="forge-pdm-mlops" width="440">
</p>

<h1 align="center">forge-pdm-mlops</h1>

<p align="center"><em>An MLOps pipeline over synthetic predictive-maintenance telemetry — train, track, register, serve, and a drift → auto-retrain loop you can watch close.</em></p>

<p align="center">
  <img src="https://img.shields.io/badge/status-F2.5%20%E2%80%94%20outlier%20robustness-yellow" alt="Status: F2.5 — outlier robustness">
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

> ⚠️ **Honest status — F2.5 (outlier robustness).** F0 (skeleton), F1 (real data layer
> + leakage-safe features) and **F2 (the training core — `pdm train` runs a two-model
> comparison and registers the winner in MLflow)** are in place. **F2.5 adds outlier
> robustness**: an unsupervised detection ladder **scored against the generator's
> ground-truth labels**, turned into a leakage-safe `signal_suspect` feature (see [the
> section below](#outlier-robustness-scored-against-ground-truth-f25)). `serve`, the
> registry promotion gate, and the drift loop land across F3–F5 — see
> [`docs/ROADMAP.md`](docs/ROADMAP.md). Nothing here implies a live production
> deployment; the drift→retrain loop, once shipped, is a **demonstrated closed loop on
> synthetic data**.

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
| **F2.6** | Tune + diagnose — grouped-CV Optuna HPO on the cleaned inputs + model diagnostics + training watchers ☐ |
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
