# Roadmap — forge-pdm-mlops

Phases ship incrementally; each ends green (offline pytest + CI) with `STATE.md`
updated and an ADR for any non-obvious choice. **MVP = F0–F2 + the F5 loop on the
fixture.** F3/F4 make it a real system; F5 is the headline; F6 is gravy.

| Phase | Status | What |
|------|--------|------|
| **F0** | ◑ in progress | Foundations & runnable skeleton (package, `pdm` CLI, CI, committed smoke fixture, canonical dataset config) |
| **F1** | ☐ | Data adapter (full regeneration + offline fixture fallback) + features (era-NULL handling, leakage guard, unit-grouped split) |
| **F2** | ☐ | Train + track — LogReg + LightGBM, both logged to MLflow, the winner registered (**MVP core**) |
| **F3** | ☐ | Model registry: register + stage→production promotion gated by an eval metric + rollback |
| **F4** | ☐ | Serving — FastAPI (`/predict`, `/health`, `/model-info`) + Dockerfile + compose (serving + MLflow UI) |
| **F5** | ☐ | **Drift monitoring + the auto-retrain loop (marquee)** — Evidently report + Prefect flow + scheduled GH Actions trigger |
| **F6** | ☐ | *(stretch)* hosted free-tier deploy (Fly.io / Render / HF Spaces) → a live `/health` link |

---

## F0 — Foundations & runnable skeleton

- **Objective.** A clone-and-run skeleton: importable package, a `pdm` CLI whose
  subcommands are honestly stubbed, green CI on Linux+Windows × 3.11/3.12, the
  committed smoke fixture, and the canonical dataset config.
- **How.** src-layout package; `pdm --version` smoke-tested in CI (mirrors the
  generator's `forge --version`); `configs/dataset.json` as cross-machine truth;
  `scripts/build_sample.py` bakes the reduced offline fixture; ADR-001 (data
  coupling) + ADR-002 (the stack & why-both-orchestrators).
- **DoD.** `pip install -e .[dev]` → `pytest` green offline; `pdm --version` works;
  CI matrix green; STATE/ROADMAP/ARCHITECTURE/DECISIONS written.

## F1 — Data + features

- **Objective.** Turn the generator's `readings` into a leakage-safe modelling frame.
- **How.** `data.py` regenerates the full dataset from `configs/dataset.json` (falls
  back to the fixture offline, loudly). `features.py` handles era-`NULL`
  missingness, asserts no target leakage, and splits **by unit** (no unit in both
  train and test). Tests on the fixture.
- **DoD.** Deterministic train/test frames; a leakage guard test; era-NULL preserved.

## F2 — Train + track (MVP core)

- **Objective.** Model-selection-as-an-MLOps-process.
- **How.** `models.py` (LogReg pipeline + LightGBM behind one `fit/predict_proba`
  interface). `train.py` logs **both** as MLflow runs (params, the primary metric,
  the model artifact) and returns the winner. `pdm train` works against a local
  file MLflow backend. ADR-003 (two-model comparison).
- **DoD.** `pdm train` produces two tracked runs + a registered winner; a
  same-seed-same-metric determinism test.

## F3 — Registry + promotion

- **Objective.** Governed model lifecycle.
- **How.** `registry.py`: register the winner, **stage→production promotion gated by
  the eval metric**, rollback. ADR-004 (promotion gate).
- **DoD.** A worse candidate does **not** promote (asserted); rollback restores the
  prior production version.

## F4 — Serving

- **Objective.** Serve the promoted model.
- **How.** `serve.py` FastAPI loads the **production** model from the registry;
  `/predict` (readings → failure probabilities), `/health`, `/model-info` (live
  version). `Dockerfile` + `docker-compose.yml` (serving + MLflow UI) — one command.
- **DoD.** `TestClient` round-trips a prediction; compose brings up both services.

## F5 — Drift monitoring + the auto-retrain loop (marquee)

- **Objective.** A demonstrated closed loop: drift detected → retrain → recovered
  model promoted.
- **How.** `monitor.py` (Evidently report baseline vs. a `--season heatwave` shift +
  a drift decision). `flows.py` Prefect flow `detect_drift → [if drift] →
  retrain(compare) → evaluate → promote-or-hold`, tasks with retries.
  `.github/workflows/retrain.yml` runs it on a schedule on cloud runners.
  ADR-005 (drift metric + retrain trigger policy). DEMO.md + a GIF.
- **DoD.** Flow runs **in-process** in tests on the fixture; the drift branch fires
  and a model is promoted; the scheduled workflow is wired.

## F6 — (stretch) hosted free-tier deploy

- **Objective.** A live link.
- **How.** Deploy the serving image to a free tier (Fly.io / Render / HF Spaces).
- **DoD.** A reachable `/health` in the README. Only if low-friction.
