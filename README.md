<p align="center">
  <img src="assets/logo.png" alt="forge-pdm-mlops" width="440">
</p>

<h1 align="center">forge-pdm-mlops</h1>

<p align="center"><em>An MLOps pipeline over synthetic predictive-maintenance telemetry — train, track, register, serve, and a drift → auto-retrain loop you can watch close.</em></p>

<p align="center">
  <img src="https://img.shields.io/badge/status-F1%20%E2%80%94%20data%20%2B%20features-yellow" alt="Status: F1 — data + features">
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

> ⚠️ **Honest status — F1 (data + leakage-safe features).** The runnable skeleton
> (F0) is in place — importable package, a `pdm` CLI whose subcommands are **honestly
> stubbed** until their phase lands, green CI on Linux + Windows × Python 3.11/3.12,
> the canonical dataset config, and a committed offline smoke fixture. **F1 adds the
> real data layer**: full-dataset regeneration from the canonical config (with a loud
> fallback to the offline fixture) and a **leakage-safe feature pipeline** — see
> [the section below](#honest-evaluation-baked-in-f1). Training, registry, serving and
> the drift loop land across F2–F5 — see [`docs/ROADMAP.md`](docs/ROADMAP.md). Nothing
> here implies a live production deployment; the drift→retrain loop, once shipped, is a
> **demonstrated closed loop on synthetic data**.

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

Determinism threads end to end — one seed → data → split → (F2) metrics. Rationale in
[ADR-003](docs/DECISIONS.md).

## The stack (and why two orchestration layers)

| Concern | Tool | Note |
|---------|------|------|
| Experiment tracking + model registry | **MLflow** | Local file backend — no server, no cost. |
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
pdm train             # F2 — train both models, track to MLflow, register the winner
pdm serve             # F4 — FastAPI serving the promoted model
pdm flow --season heatwave   # F5 — the drift → retrain loop (the marquee)
pdm monitor           # F5 — an Evidently drift report
```

## Roadmap

| Phase | What |
|------|------|
| **F0** | Foundations & runnable skeleton (package, CLI, CI, canonical config, smoke fixture) ✅ |
| **F1** | Data adapter (full regeneration + offline fallback) + leakage-safe features ✅ |
| **F2** | Train + track — two models to MLflow, the winner registered (**MVP core**) ☐ |
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
