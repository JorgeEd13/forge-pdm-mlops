# PLAN — forge-pdm-mlops

## Why this project

The 4th public showcase, built to close the **MLOps-in-production gate** (experiment
tracking, model registry, serving, drift monitoring, orchestration) with a navigable,
clickable artifact instead of a claimed "interest". It is the downstream half of the
[`can-telemetry-forge`](https://github.com/JorgeEd13/can-telemetry-forge) narrative: *built the data engine,
then the ML-in-production system over it.* The marquee is a **closed drift →
auto-retrain loop** — the generator's `season` knob shifts the distribution, the
pipeline detects it and retrains a recovered model.

Constraints (inherited from the showcase line): clean-room, 100% synthetic data,
English everywhere, plan-first with ADRs, README-is-half-the-product, pytest + green
CI from the start, honest design-vs-shipped framing. No GPU, no paid cloud, free CI.

## Locked decisions

- **Data coupling (ADR-001).** Pinned generator dependency **+** a committed smoke
  fixture; one canonical `configs/dataset.json` reproduces the **full** dataset
  identically on every machine; models train on the full dataset only, never the
  reduced fixture.
- **Stack (ADR-002).** MLflow (tracking + registry) · FastAPI (serving) · Evidently
  (drift) · **Prefect** (the orchestration DAG) · **GitHub Actions** (scheduled cloud
  trigger). Two orchestration layers that compose, not duplicate — each closes a
  distinct gate.
- **Model (ADR-003, F2).** scikit-learn `LogisticRegression` baseline + LightGBM
  contender, compared through MLflow; the winner is promoted. Cheap, no GPU; the
  pipeline is the product, not the model.
- **Target.** `failure_within_h` from the generator's `readings` table.

## Phases

`F0` skeleton + CI + canonical config + fixture · `F1` data + leakage-safe features ·
`F2` train + track (MVP core) · `F3` registry + gated promotion · `F4` serving ·
`F5` **drift → retrain loop (marquee)** · `F6` *(stretch)* hosted free-tier deploy.
Details + definitions of done in [`docs/ROADMAP.md`](docs/ROADMAP.md).

**MVP = F0–F2 + the F5 loop on the fixture.** One phase per session.

## Verify

- **Offline / CI:** `pip install -e .[dev]` → `pytest` green on the committed fixture;
  MLflow → tmp file backend; Prefect in-process; CI matrix Linux+Windows × 3.11/3.12.
- **Local full loop (F2+):** `pdm train` → `pdm serve` → `curl /predict` →
  `pdm flow --season heatwave` detects drift, retrains, promotes a recovered model.
- **Determinism:** same seed → identical metrics (asserted).
