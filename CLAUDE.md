# CLAUDE.md — conventions for AI-assisted development

Guidance for any AI collaborator (and humans) in this repo. Read this first, then
[`docs/STATE.md`](docs/STATE.md) for where things stand.

## What this project is

`forge-pdm-mlops` is a **public portfolio project**: an MLOps pipeline over
synthetic predictive-maintenance telemetry. Its data source is the companion
**`can-telemetry-forge`** generator (J1939-grounded heavy-equipment CAN data). It
trains a failure classifier, tracks experiments and registers models with
**MLflow**, serves the promoted model with **FastAPI**, and closes a **drift →
auto-retrain** loop with **Evidently** + **Prefect** (the generator's `season` knob
is the drift stimulus). A scheduled **GitHub Actions** workflow runs the loop on
free cloud runners.

It is the **4th public showcase** (after `receivables-agent`, `machine_scanner`,
and `can-telemetry-forge`) and the downstream half of the can-telemetry-forge
narrative: *built the data engine, then the ML-in-production system on top of it.*

## How to resume (reading order)

1. [`docs/STATE.md`](docs/STATE.md) — current focus, done, next step. **Always.**
2. [`docs/ROADMAP.md`](docs/ROADMAP.md) — phases F0…F6 with Objective / How / DoD.
3. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — module design + data flow.
4. [`docs/DECISIONS.md`](docs/DECISIONS.md) — ADRs (data coupling, stack, …).
5. [`PLAN.md`](PLAN.md) / [`README.md`](README.md) — plan + product-level usage.

## Golden rules

1. **English everywhere.** Names, comments, commits, docs.
2. **Clean room.** No proprietary code or data, ever. The data is 100% synthetic,
   from the public generator; the only "real" dataset (VED, in the generator) is a
   license-checked public one used over there, never here.
3. **The committed sample is a smoke fixture, NOT a training set.** Models are
   always trained on the **full** dataset regenerated from
   [`configs/dataset.json`](configs/dataset.json) — the single cross-machine source
   of truth. `data/sample_readings.parquet` exists only so `clone && pytest` and CI
   run offline. Never fit a reported model on the reduced fixture (ADR-001).
4. **One dataset across machines.** Desktop and notebook must produce
   byte-identical data: same `configs/dataset.json` + the **pinned** generator
   version (`[generate]` extra). Don't fork the config per machine.
5. **Reproducible & deterministic.** One seed threads data → split → train. Same
   seed → same metrics. No bare global randomness.
6. **Honest output.** Mark design vs. shipped per phase; the drift→retrain loop is
   a *demonstrated closed loop on synthetic data*, not a live production claim.
7. **Plan before you code.** Record non-obvious choices as ADRs in `docs/DECISIONS.md`.
8. **Token economy.** Volatile state in `docs/STATE.md`; durable design in
   `docs/ARCHITECTURE.md`; decisions in `docs/DECISIONS.md`.

## Where things live (target layout)

- `src/pdm_mlops/data.py`     — generator adapter (full dataset) + offline fixture fallback
- `src/pdm_mlops/features.py` — feature/target prep (era-NULL handling, leakage guard, split)
- `src/pdm_mlops/models.py`   — LogReg baseline + LightGBM contender, one interface
- `src/pdm_mlops/train.py`    — train both → MLflow tracking → pick the winner
- `src/pdm_mlops/registry.py` — MLflow registry: register + gated promotion + rollback
- `src/pdm_mlops/serve.py`    — FastAPI serving the promoted model
- `src/pdm_mlops/monitor.py`  — Evidently drift report + drift decision
- `src/pdm_mlops/flows.py`    — Prefect drift → retrain flow
- `src/pdm_mlops/config.py`   — paths, MLflow wiring, seeds, thresholds
- `src/pdm_mlops/cli.py`      — `pdm` entry point
- `configs/dataset.json`      — canonical training-dataset spec (cross-machine truth)
- `scripts/build_sample.py`   — regenerate the offline smoke fixture (authoring-time)
- `tests/`                    — offline, deterministic pytest
- `docs/`                     — STATE, ROADMAP, ARCHITECTURE, DECISIONS, DEMO

## Conventions

- Python ≥ 3.11, type hints, `from __future__ import annotations`.
- All randomness flows through a seeded generator passed down — never a global call.
- MLflow uses a local file backend (`mlruns/`, git-ignored); no server, no paid service.
- Unit tests stay offline and deterministic (the committed fixture, MLflow → tmp,
  Prefect in-process); CI never installs the `[generate]` extra or hits the network.

## Watch for portfolio-worthy findings

Career-showcase repo. When something genuinely CV/post-worthy appears (a clean MLOps
technique, a shipped capability, a number), note it for
`(private career repo)` — even unprompted. Sanitize to the public level
(it is already clean-room; tools/methods are free to name).

## Definition of done (per feature)

- Code + type hints + a focused offline, deterministic test.
- `docs/STATE.md` updated; phase marked in `ROADMAP`.
- An ADR in `docs/DECISIONS.md` if a non-obvious choice was made.
