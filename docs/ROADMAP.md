# Roadmap — forge-pdm-mlops

Phases ship incrementally; each ends green (offline pytest + CI) with `STATE.md`
updated and an ADR for any non-obvious choice. **MVP = F0–F2 + the F5 loop on the
fixture.** F3/F4 make it a real system; F5 is the headline; F6 is gravy.

| Phase | Status | What |
|------|--------|------|
| **F0** | ✅ done | Foundations & runnable skeleton (package, `pdm` CLI, CI, committed smoke fixture, canonical dataset config) |
| **F1** | ✅ done | Data adapter (full regeneration + offline fixture fallback) + features (era-NULL handling, leakage guard, unit-grouped split) |
| **F2** | ✅ done | Train + track — LogReg + LightGBM, both logged to MLflow, the winner registered (**MVP core**) |
| **F2.5** | ✅ done | **Outlier robustness (clean first)** — unsupervised multivariate + temporal + autoencoder ladder on signals, scored vs. ground truth (AE earns its place; temporal rewritten after a logged negative result) → a leakage-safe `signal_suspect` feature + a data-quality watcher |
| **F2.6** | ✅ done | Tune + diagnose — CV-grouped Optuna HPO on the cleaned inputs + logged model diagnostics + training watchers (instrumentation, not accuracy theatre) |
| **F2.7** | ☐ proposed | **Temporal modelling — does the trajectory help?** A three-rung ladder (per-row LightGBM → temporal-features LightGBM → a dilated **causal** TCN), the deep rung must *earn its place*; representation, not tuning, is the lever (ADR-007) |
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
  SQLite MLflow backend (ADR-004 — the file store is in maintenance mode in MLflow 3).
- **DoD.** ✅ `pdm train` produces two tracked runs + a registered winner; a
  same-seed-same-metric determinism test; a `DegenerateSplit` guard for the fixture.

## F2.5 — Outlier robustness (clean first)

- **Objective.** Make the pipeline robust to the **dirty inputs the generator injects
  on purpose** — and *prove* it against ground truth. The generator labels nine defect
  families split into **obvious** (`obvious_outlier` — a range spike) and **subtle**
  (`joint_outlier`: each signal plausible, the *combination* implausible; `sensor_stuck`:
  in-range but frozen; `sensor_drift`: slow creep; the `can_frame_*` faults). A serious
  PdM model has to survive these; cleaning *before* tuning is the right ML order (F2.6
  tunes on the cleaned frame). **This pillar is only measurable because the generator
  labels the outliers** — the two repos tell one story.
- **The hard honesty rule.** Detection runs on the **feature signals ONLY**; the
  `is_outlier`/`anomaly_type` labels are used **solely to *score*** the detector, never
  as a model input (the F1 leakage guard, ADR-003, stays sacred). Robustness is earned
  from raw signals exactly as it would be in production.
- **How — a detection ladder, each rung scored vs. ground truth:**
  - **Multivariate (joint outliers).** `IsolationForest` + a robust-covariance
    **Mahalanobis** distance over the signals — catches "this *combination* is
    implausible" (700 rpm at 95% load), which per-column checks miss.
  - **Temporal (stuck/drift).** Per-unit rolling features (rolling variance → ~0 =
    stuck; rolling slope → steady nonzero = drift). Thresholds are a **domain call made
    with Jorge** against the labeled data, not auto-picked.
  - **Autoencoder (the adaptive headline, Jorge's call to include now).** A small
    CPU-only PyTorch autoencoder trained on *normal* joint+temporal patterns;
    reconstruction error = suspicion, so it can flag patterns we never explicitly
    designed for. Seeded/deterministic; new `[deep]` extra (CPU torch), **kept out of
    core CI**. It must **earn its place**: scored against ground truth like the cheaper
    rungs, and we report whether it measurably beats them on *subtle* recall — if it
    doesn't, that's documented honestly, not hidden.
  - **Output: a leakage-safe `signal_suspect` feature.** The detectors' (label-free)
    suspicion score becomes a new model feature so the downstream classifier can learn
    to distrust suspect values. Plus a **data-quality watcher** that fails loud if a
    batch's outlier rate spikes (doubles as an F5 drift signal).
  - **ADR-005** — the detection ladder, the signals-only/score-only honesty rule, the
    cleaning policy (`signal_suspect` feature), and the autoencoder "earn-its-place"
    decision + result.
- **DoD.** ✅ A reported, ground-truth-scored detection table (recall on **obvious vs.
  each subtle family**, detector by detector); the autoencoder compared head-to-head
  with the cheap methods on subtle recall (**it earns its place**); a leakage-safe
  `signal_suspect` feature that the leakage guard still passes; the data-quality watcher
  fires in a test; everything seeded/deterministic and offline on the fixture.
- **Shipped.** `detect.py` (ladder), `detect_score.py` (tie-aware ground-truth scoring),
  `suspect.py` (`signal_suspect` feature + data-quality watcher), `pdm detect`, the
  `[deep]` torch extra (out of core CI), ADR-005. The temporal rung was **rewritten**
  after its rolling-variance/slope form scored ~0.02 F1 — the diagnosis (stuck = an
  exact-value freeze on an unsupervised-selected continuous signal; drift = a sustained
  monotone creep on a non-monotone signal) is the fix, and the negative result is logged
  in ADR-005, not hidden. 22 new offline tests (49 total green).

## F2.6 — Tune + diagnose (instrumentation)

- **Objective.** On the **cleaned** inputs from F2.5, make "why this model, with these
  params" **visible and defensible**, not asserted — and instrument training so a bad
  fit fails loud. *Not* an accuracy play: on this synthetic data the score is faint by
  design; the value is the tracked search + diagnostics + guards.
- **How.**
  - **HPO (`tune.py`).** An **Optuna** study (small budget, ~30–50 trials, seeded)
    over each model's param space, scored by **unit-grouped CV** (`GroupKFold`) so the
    search can't leak units across folds and undo ADR-003. Logged to MLflow; `pdm tune`
    runs it; tuned params feed `train`. New `[tune]` extra (`optuna`).
  - **Diagnostics.** Per fitted model, log to its MLflow run: a learning curve, feature
    importance (LightGBM gain / LogReg |coef|), a calibration check, and a threshold
    sweep (precision/recall vs. threshold). Artifacts, not just scalars.
  - **Training watchers** (forensic-watcher pattern): `DegenerateSplit` (shipped), an
    **overfit-gap** guard (train − CV AUC over a threshold), and a **majority-baseline**
    guard (test AUC must beat the majority class). Opt-in `--audit`.
  - **ADR-006** — HPO method (Optuna + grouped CV), the diagnostics set, the watcher
    policy, the honesty note (instrumentation over accuracy on synthetic data).
- **DoD.** ✅ `pdm tune` produces a tracked Optuna study with grouped CV; diagnostics land
  as MLflow artifacts; the overfit-gap + majority-baseline watchers fire in a test;
  tuned params reproduce (same seed → same best params).
- **Shipped.** `tune.py` (grouped-CV Optuna study per model, cleaned frame, tracked),
  `diagnostics.py` (importance/calibration/threshold/learning-curve artifacts + the
  overfit-gap & majority-baseline watchers), validated `overrides` on the model builders,
  `pdm tune` + `pdm train --tune/--audit/--diagnose/--clean`, the `[tune]` extra, ADR-006.
  An honest finding kept: on the 15-unit smoke fixture the deep model genuinely overfits
  (train ≈1.0 vs. grouped-CV ≈0.6), so the overfit watcher **trips** — that is the guard
  earning its keep (a fixture-size artifact, like `DegenerateSplit`), tested as such. 14
  new offline tests (63 total green).

## F2.7 — Temporal modelling (does the trajectory help?)

- **Objective.** F2.6 *measured* that tuning is exhausted (+0.003). The ceiling is a
  **representation** limit: the failure is a progressive degradation **ramp** (generator
  ADR-020), and a per-row model discards the trajectory that is the signal. So give a model
  the temporal structure and **measure whether it earns the complexity** — not chase the
  number with a bigger classifier. Also fills a real gap: a *public* PyTorch deep showcase
  (the only sequence work, `project_fleet_ml`, is private).
- **How — a three-rung ladder, same unit split / seed / metric / test rows (apples-to-apples):**
  - **(a) per-row LightGBM** — the F2.6 ceiling (0.8152), the bar.
  - **(b) temporal-features LightGBM** — per-unit rolling/lag window stats (mean/slope/std)
    → the *same* LightGBM. Isolates "does temporal structure help **at all**" (cheap, CPU),
    and is the bar the deep rung must clear (so we never conflate "temporal helps" with
    "deep helps").
  - **(c) a dilated *causal* TCN** (PyTorch) over per-unit windows — must **earn its place**
    over (b), reported either way (the F2.5 autoencoder pattern). Causal convolutions
    *structurally* forbid intra-window future leakage; era-NULL enters as impute + a
    **missingness-mask channel**; every test row scored (left-pad short histories).
  - **ADR-007** — why representation-not-tuning, the ladder, the TCN choice, the
    parallel-contender integration (`sequence.py`, not forced into `build_all`), determinism
    (tested path tiny/CPU; reported run GPU — RTX 4050), the `[deep]` extra reuse.
- **DoD.** A reported three-way comparison on the same test set (the deep rung measured to
  earn — or not earn — its place over temporal-features); the windowing's unit-grouped split
  + no-future-leak asserted by test; same-seed determinism; everything offline/deterministic
  on the fixture (a tiny CPU TCN); logged to the same MLflow experiment, winner registrable.
- **Note.** A modelling phase, **orthogonal to the MLOps gate** (F3/F4/F5). Sits before F3 in
  execution but does not block it; no schedule pressure (Jorge's call, 2026-06-27).

## F3 — Registry + promotion

- **Objective.** Governed model lifecycle.
- **How.** `registry.py`: register the winner, **stage→production promotion gated by
  the eval metric**, rollback. ADR-008 (promotion gate).
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
  ADR-009 (drift metric + retrain trigger policy). DEMO.md + a GIF.
- **DoD.** Flow runs **in-process** in tests on the fixture; the drift branch fires
  and a model is promoted; the scheduled workflow is wired.

## F6 — (stretch) hosted free-tier deploy

- **Objective.** A live link.
- **How.** Deploy the serving image to a free tier (Fly.io / Render / HF Spaces).
- **DoD.** A reachable `/health` in the README. Only if low-friction.
