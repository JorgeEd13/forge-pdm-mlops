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
| **F2.7** | ☑ done | **Temporal modelling — does the trajectory help?** A three-rung ladder (per-row LightGBM → temporal-features LightGBM → a dilated **causal** TCN). **Measured (full data, GPU, seed 42): per-row 0.8125 → temporal-features 0.8194 (+0.0069, temporal *does* help) → TCN 0.8148 (−0.0046, deep does NOT earn its place; HPO of the TCN, grouped-CV, lands 0.8107 — tuning doesn't rescue it).** The cheap, interpretable temporal-features LightGBM wins; reported either way (ADR-007) |
| **F2.8** | ☑ built, offline-tested *(full-data numbers pending a GPU `pdm ceiling` run)* | **Characterize the ceiling — is the limit the data or the model?** Stops *asserting* "0.82 is the information limit" and **measures** it: `ceiling.py` runs a per-horizon + per-failure-mode AUC decomposition, a **fenced** label-leaking upper-bound (a diagnostic, never a reported metric — the fence is asserted by test), and an **OOF unit-grouped stacking redundancy probe** over the F2.7 rungs (can't beat its best base ⇒ rungs redundant ⇒ ceiling is the data). CPU-only on the desktop; the GPU TCN folds into the probe through an `extra_oof` seam. **The capstone that closes the F2.* modelling arc** (ADR-010) |
| **F2.9** | ↗ future work *(deferred by design)* | **Task reframing — does the binary target hide the ramp?** Reframe to **RUL / graded severity** (the PdM task where the trajectory carries separable signal) and re-run the ladder + a stack. Identified and scoped, **intentionally not built**: it's a deep-learning/modelling axis better owned by a dedicated DL showcase than buried in this MLOps repo — the spine (F3+) is the priority (ADR-011) |
| **F2.10** | ↗ future work *(deferred by design)* | **Cross-dataset validation — does the conclusion generalize?** Run the same ladder on **NASA C-MAPSS** (the canonical public RUL benchmark where temporal models win). Scoped, **intentionally not built** for the same reason as F2.9 — a generalization claim worth making in its own focused artifact, not as a sub-phase of the production showcase (ADR-012) |
| **F3** | ✅ done | Model registry governance: metric-gated **promotion** to a `production` **alias** (MLflow 3, not deprecated stages) + **rollback** to the prior version. **A worse candidate does not promote** (asserted); rollback restores the previous production version (ADR-008) |
| **F4** | ✅ done | Serving — FastAPI (`/predict`, `/health`, `/model-info`) over the `production`-**aliased** model + Dockerfile + compose (serving + MLflow UI). A promotion/rollback (F3) changes what `/predict` answers with **no redeploy**; probabilities via the native flavor; `TestClient` round-trips a prediction (ADR-009) |
| **F5** | ✅ done | **Drift monitoring + the auto-retrain loop (marquee)** — `monitor.py` (Evidently `DataDriftPreset` over the feature signals + a **share-threshold** drift decision) + `flows.py` (a Prefect `detect_drift → [if drift] → retrain → promote-or-hold` flow that routes every promotion through the **same F3 gate**, so auto-retrain can't auto-degrade) + the scheduled GH Actions trigger. Runs **in-process** on the fixture; `pdm monitor` / `pdm flow` live (ADR-013) |
| **F6** | ✅ done | *(stretch)* hosted free-tier deploy (Hugging Face Spaces) → a live `/health` link. A **self-contained** `Dockerfile.hf` **bakes a fixture-trained demo registry** at build time (train → register → promote through the same F3 gate), so a fresh cloud deploy serves a real prediction and `/health` shows `model_loaded=true` — labelled a **demo** everywhere (ADR-001 boundary intact: a fixture model is *served, never reported*). `scripts/seed_demo_registry.py` + `docs/DEPLOY.md`; `retrain.yml` now runs `pdm flow` for real. The ≈0.82 full-data model is what `pdm train` produces locally (ADR-014) |
| **F7** | ✅ **LIVE** — [Cloud Run + Neon](https://forge-pdm-mlops-958199756179.us-central1.run.app/demo), $0 (ADR-016) | **Managed-cloud deploy — the managed-cloud gate.** The **same** `Dockerfile.hf` image on **Google Cloud Run** (a *managed* container runtime, not a VM) + a **managed Postgres** as a **managed resource** — **Neon** (free-tier serverless Postgres; Cloud SQL is the paid alternative, ADR-016). An interactive **demo UI** (`/demo`: set the J1939 parameters → get the probability) gives the DB an honest job — every served prediction is **logged to the managed Postgres** (`persisted:true`) and read back into a recent-predictions panel. **Graceful degrade:** no `DATABASE_URL` ⇒ no persistence, so local/HF/CI are unaffected; the same `store_pg` code runs on tmp SQLite in tests. No PII, no committed secrets (Secret Manager). The `demo=fixture` honesty boundary intact. Closes the gate F0–F6 left open: *containerize ≠ operate managed cloud* (ADR-015/016) |
| **F8** | ✅ done | **Bring-your-own-data demo.** Upload a CAN/J1939 batch (CSV/Parquet) to `/demo` → per-row failure probabilities + a summary. Completes the interactive-demo vision (F7 = single-row tuning; F8 = "bring your own dataset"). A **map-your-columns step** (fuzzy auto-match, stdlib `difflib` + a J1939 synonym table — **no new dep** — + manual override) makes **arbitrary header names** work, and a partial dataset scores with missing signals as era-`NULL` ("N of 9 provided"). New pure `upload.py` (parse/suggest/build/validate/summarize) wrapped by one `POST /demo/upload` (preview → confirmed-mapping score); `_score` refactored to a shared `_score_frame` core. Guardrails fail loud (size/row caps, non-J1939 file, no-mapping) as 4xx, never 500. `demo=fixture` banner + **no-raw-row-persistence** intact; 25 offline tests (ADR-017) |
| **F9** | ☐ planned | **Demo product-polish.** Friendlier `/demo`: **preset example buttons** (healthy/failing) + units/tooltips + bounded inputs + a **risk-meter** result (fixes "too technical"); **light/dark theme** (theme-aware, keeps the no-CDN constraint); **EN/PT-BR i18n** of the UI shell. A front-end/product-showcase play, **paired with `receivables-agent` Phase 9** for a shared design language (do them together — same theme/i18n code where viable). Honesty banner + reported-number framing intact in both themes/languages. ADR-018 when built. |

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
- **DoD — met.** A reported three-way comparison on the same test set (the deep rung measured
  to **not** earn its place: TCN 0.8148 < temporal-features 0.8194; temporal *does* help,
  +0.0069 over per-row); the windowing's unit-grouped split proven **row-identical to F1** +
  no-future-leak asserted by test; same-seed determinism (offline CPU **and** the GPU run);
  everything offline/deterministic on the fixture (a tiny CPU TCN); logged to the same MLflow
  experiment, winner registrable. `sequence.py` + `pdm sequence` + 10 tests. **73 green.**
- **Note.** A modelling phase, **orthogonal to the MLOps gate** (F3/F4/F5). Sits before F3 in
  execution but does not block it; no schedule pressure (Jorge's call, 2026-06-27).

## The F2.8–F2.10 modelling arc — "is the model the bottleneck?", measured to exhaustion

F2.6 and F2.7 turned one question into measured answers (tuning? no; representation? a little;
deep? no; tuning the deep? no). The investigation is closed with **F2.8 as its capstone** — a
*measurement* that 0.82 is an information ceiling, not a modelling one — and stops there **by
design**.

**F2.9 and F2.10 are scoped but deliberately deferred (2026-06-27 decision, career-wide view).**
The reasoning: this repo's job is the **MLOps production spine** (train → registry → serve →
drift → retrain → cloud), and the rare portfolio signal is *finishing that spine*, not polishing
a modelling side-quest. F2.5/F2.6/F2.7 already proved the rigor/honesty attitude conclusively;
two more sub-phases reinforce the same trait at diminishing return while an over-deep F2 branch
next to an unfinished gate *inverts* the signal. RUL (F2.9) and C-MAPSS (F2.10) are a
deep-learning/benchmarking axis better owned by a **dedicated DL showcase** (or by making the
private `project_fleet_ml` browsable) than buried here. Leaving them as *curated future work* is
itself the senior signal — knowing what the next steps are and choosing the spine. They are kept
below in full so the judgment (and the scoping) is on the record.

## F2.8 — Characterize the ceiling (is the limit the data or the model?)

- **Objective.** Stop *asserting* "0.82 is the data's information limit" and **measure** it —
  where predictability lives, and how much of the gap to a perfect score is irreducible.
- **How.** (1) **Decompose the score** — held-out AUC by **time-to-failure horizon** bucket and
  by **failure mode**: is 0.82 flat, or ~0.95 near failure and ~0.6 far out (most of the 168h
  window genuinely healthy-and-unpredictable by construction)? (2) A deliberately **label-leaking
  upper-bound** model (sees `failure_mode`/time-to-failure) — **a diagnostic, clearly labelled,
  never reported as a result** — to bound the irreducible error. (3) A **stacking redundancy
  probe**: an out-of-fold meta-learner over the F2.7 rungs; if it fails to beat its best member,
  the rungs are information-redundant → the ceiling is the data, confirmed. Offline-testable on
  the fixture; full numbers on GPU/full data.
- **DoD.** A reported horizon/mode decomposition; a measured irreducible-error bound (with the
  leaky model fenced off from any reported metric, asserted by test); the stacking probe's
  verdict reported either way. **ADR-010.**

## F2.9 — Task reframing: RUL / graded label (does the binary target hide the ramp?) — *future work, deferred by design*

> **Status: scoped, intentionally not built.** Kept on record as the next rigorous modelling
> step; deferred so the production spine (F3+) lands first and so this DL/modelling axis can live
> in a focused showcase rather than this MLOps repo (2026-06-27).

- **Objective.** The binary `failure_within_h` flattens a continuous degradation **ramp** into a
  step. Reframe to **remaining-useful-life (RUL) regression** or a **graded severity** target —
  the canonical PdM framing where the *trajectory* carries separable signal a per-row snapshot
  can't. The one honest swing at actually moving the result: by matching the **task** to the
  information, not the **model** to the test.
- **How.** Derive a continuous RUL / graded target from the generator's failure event
  (label-side, **leakage-safe** — features stay signals-only, the F1 guard holds on the new
  target). Re-run the same ladder (per-row / temporal-features / TCN) **plus the OOF stack** from
  F2.8, on the same unit split / seed. Metric matched to the task (RUL: MAE/RMSE + a banded-AUC
  for comparability with F2.7; or an ordinal score). **Stacking can finally earn its place here**
  if temporal/deep stop being redundant.
- **DoD.** A reported comparison on the reframed task; whether temporal/deep/stack now earn their
  place (reported **either way** — a null is still the result); the leakage guard re-asserted on
  the new target. **ADR-011.**

## F2.10 — Cross-dataset validation: NASA C-MAPSS (does the conclusion generalize?) — *future work, deferred by design*

> **Status: scoped, intentionally not built.** A generalization claim worth making in its own
> focused artifact, not as a sub-phase of the production showcase; deferred with F2.9 (2026-06-27).

- **Objective.** Test whether "temporal helps a little, deep doesn't earn its place" is a
  property of **this synthetic data's ceiling** or a general claim — by running the same ladder on
  **NASA C-MAPSS** (the canonical public turbofan **RUL** benchmark, where temporal models *are*
  known to win). If the rung ranking **flips** there, the pipeline was never the bottleneck — the
  synthetic ceiling was. A deliberate, **eyes-open scope expansion** of the repo's narrative.
- **How.** A thin, **license-checked public** C-MAPSS adapter into the same `features` / `sequence`
  surfaces (kept clearly separate from the synthetic story — a `benchmarks/` path), then the same
  three-rung ladder + stack. Compare the **ranking** of rungs on C-MAPSS vs. here, not absolute
  numbers across datasets.
- **DoD.** A reported ladder on C-MAPSS; an explicit statement of whether the deep rung wins there
  (confirming the synthetic ceiling, not a pipeline limit); clean-room boundary intact (public
  data only, never mixed into the synthetic narrative). **ADR-012.**

## F3 — Registry + promotion ✅

- **Objective.** Governed model lifecycle.
- **How.** `registry.py`: **metric-gated** promotion of a registered version to a
  `production` **alias** (MLflow 3 deprecated the classic stages — ADR-008) + **rollback**
  to the version it superseded (recorded as a tag for determinism). A rejection is a
  structured governed *outcome*, not an exception; malformed requests raise. `pdm promote`
  (`--version`/`--min-delta`/`--force`) + `pdm rollback`.
- **DoD — met.** A worse candidate does **not** promote (asserted); rollback restores the
  prior production version (asserted). `test_registry.py` (14, offline, tmp SQLite) also
  covers first-promotion, ties, `min_delta` tolerance, `--force`, and loud errors on
  malformed input. **ADR-008.**

## F4 — Serving

- **Objective.** Serve the promoted model.
- **How.** `serve.py` FastAPI loads the **production** model from the registry;
  `/predict` (readings → failure probabilities), `/health`, `/model-info` (live
  version). `Dockerfile` + `docker-compose.yml` (serving + MLflow UI) — one command.
- **DoD.** `TestClient` round-trips a prediction; compose brings up both services.

## F5 — Drift monitoring + the auto-retrain loop (marquee) ✅

- **Objective.** A demonstrated closed loop: drift detected → retrain → recovered
  model promoted — that **cannot silently ship a worse model**.
- **How.** `monitor.py` (Evidently `DataDriftPreset` over the feature signals baseline
  vs. a `--season heatwave` shift + a **share-threshold** drift decision). `flows.py`
  Prefect flow `detect_drift → [if drift] → retrain → evaluate → promote-or-hold`,
  retried tasks. `.github/workflows/retrain.yml` runs it on a schedule on cloud runners.
  ADR-013 (drift metric + retrain trigger policy).
- **DoD — met.** The flow runs **in-process** in tests on the fixture; the drift branch
  fires and a model is promoted; a held candidate (`min_delta=-1.0`) proves the F3 gate
  still guards the automated path; the scheduled workflow is wired.
- **Shipped.** `monitor.py` (Evidently report + `DriftReport` + the `DRIFT_SHARE_THRESHOLD`
  policy), `flows.py` (the Prefect flow composing F5 monitor + F2 train + **F3's unchanged
  promote gate** → a structured `FlowResult`), `pdm monitor` + `pdm flow` (the last two
  roadmap stubs, now live), the `[ops]` extra capped `evidently<0.7`, ADR-013. 11 new
  offline tests (`test_monitor.py` 6 + `test_flows.py` 5), both `importorskip`-ing the
  `[ops]` libs so core CI stays light. **The production spine now runs end to end.**

## F6 — (stretch) hosted free-tier deploy ✅

- **Objective.** A live link — the serving layer as a reachable artifact, not just a
  `docker compose up` claim.
- **How.** A **self-contained** image (`Dockerfile.hf`, distinct from the F4 mounted-volume
  `Dockerfile`) that **bakes a demo registry at build time**
  (`scripts/seed_demo_registry.py`: train on the fixture → register → promote to `production`
  through the same F3 gate), so a hosted deploy — which has no volume to mount and an empty
  registry — still serves a real prediction on boot. Target **Hugging Face Spaces** (a Docker
  Space, permanent free URL). `docs/DEPLOY.md` has the Space front-matter + push steps + a local
  build smoke-test + Render/Fly.io alternatives on the same image.
- **The honesty boundary (ADR-014).** The baked model trains on the **smoke fixture** (the build
  is offline), which ADR-001 forbids *reporting* — but ADR-001 forbids reporting, not *serving*:
  this is a **demo model for the live endpoint**, tagged `demo=fixture`, exposed as such by
  `/model-info` and the README. The real ≈0.82 model is the local `pdm train` one; that number is
  the only one ever quoted.
- **Also.** `retrain.yml` (an F5 placeholder until now) runs `pdm flow` for real on the cron —
  installs `[ops,generate]`, uses the **real gate** (no `--min-delta` escape), so the
  cloud-scheduled loop can't auto-degrade either.
- **DoD — met.** A `Dockerfile.hf` that bakes a promoted demo model; a fresh serving process over
  the baked store returns `model_loaded=true` + a real `/predict` (verified natively; the
  container build runs on HF's runners); `test_seed_demo_registry.py` (4, offline) asserts the
  promote/serve/self-contained/deterministic behaviour; `docs/DEPLOY.md` documents the reachable
  `/health` and the README carries the link once the Space is pushed. **ADR-014.**

## F7 — Managed-cloud deploy (Cloud Run + Cloud SQL) — the managed-cloud gate

- **Objective.** Close the one gate F0–F6 deliberately left open: **operate a managed cloud
  runtime with a managed resource in production**. HF Spaces (F6) is *free hosting* —
  "containerize an app", table stakes. F7 is the senior claim: a **managed container platform**
  (Cloud Run — serverless, scale-to-zero, revisions, managed TLS) plus a **managed relational
  database** (Cloud SQL for Postgres). "An image that builds" explicitly does **not** cover this.
- **How.** Deploy the **same self-contained `Dockerfile.hf`** (already `$PORT`-aware — the
  entrypoint reads `${PORT:-8000}`, and Cloud Run injects `$PORT`) to Cloud Run via Cloud Build
  (no local Docker daemon). Give the managed DB an honest job with an interactive **demo UI**:
  `GET /demo` (a self-contained inline-CSS/JS "set parameters → get the failure probability" page,
  the click-and-try pattern) + `POST /demo/predict` (scores **and** logs each row to Cloud SQL);
  the page reads the recent predictions back. New `src/pdm_mlops/store_pg.py` (SQLAlchemy Core
  append/read log) + the `[cloud]` extra; `scripts/deploy_cloudrun.sh` (Artifact Registry + Cloud
  Build + Cloud SQL + Secret Manager + `gcloud run deploy`). **ADR-015.**
- **Hard invariants.** **Graceful degrade** — `store_pg.open_log()` returns `None` when
  `DATABASE_URL` is unset (local / HF / CI), so the demo runs without persistence and **nothing
  about serving depends on the DB**; the SAME code runs on tmp SQLite in tests and Cloud SQL in
  prod. **No PII** — the log stores only the synthetic J1939 signal values (restricted to
  `FEATURE_COLUMNS`), the probability, the model version, a UTC timestamp; no user identity.
  **No committed secrets** — the password is generated at deploy and lives in Secret Manager.
  **Honesty boundary intact** — the served model is still the `demo=fixture` model, banner on the
  `/demo` page; the only reported number is the ≈0.82 full-data local model.
- **DoD.** The `/demo` page renders with the honesty banner and every signal field; `/demo/predict`
  round-trips exactly like `/predict` (and 503s the same when nothing is promoted); with a log
  injected the served rows **persist** and surface on the page; with no log the demo still works
  and says so. `test_store_pg.py` (8, `[cloud]`-gated) + `test_demo.py` (`[serve]`+`[cloud]`-gated)
  green offline. **DONE — LIVE 2026-07-04** at
  https://forge-pdm-mlops-958199756179.us-central1.run.app (`/demo`, `persisted:true`), on
  **Cloud Run + Neon** at $0 (Cloud SQL had no free tier — see the cost pivot). The README
  carries the `/demo` link. **ADR-015 + ADR-016.**

## F8 — Bring-your-own-data demo: upload a CAN/J1939 batch → scored predictions — *planned, next phase*

- **Objective.** Complete the interactive-demo vision: today a tester can **open** `/demo` and
  **customize the parameters of one row** → get a probability (F7, done). F8 adds the missing third
  capability — **upload your own batch** of J1939 readings (CSV/Parquet) and get per-row failure
  probabilities back, so a tester can bring *their* data, not just tune the seeded sliders.
- **What already exists (so F8 is mostly a UI + parse + validation layer).** The batch **scoring
  core is already shipped**: `POST /predict` takes a list of J1939 rows and returns per-row
  probabilities through the shared `_score` core (era-`NULL` supported, columns reindexed to
  `FEATURE_COLUMNS`, `assert_no_leakage` re-run). F8 wraps that with an upload surface.
- **How (scoped).** `POST /demo/upload` (multipart) → parse CSV/Parquet (pandas) → **map the
  tester's columns onto `FEATURE_COLUMNS`** (see the column-mapping step below) → **bound the input**
  (max rows / max file size, a hard cap so a huge upload can't wedge a scale-to-zero instance) →
  score via `_score` → return per-row probabilities **plus a small summary** (n rows, % flagged
  high-risk at a stated threshold, maybe a probability histogram). The `/demo` page gains a file-drop
  control alongside the existing form. Optionally log an **aggregate** row to the managed Postgres
  (not every uploaded row — keep the no-PII, bounded-write posture; an uploaded dataset is arbitrary,
  so store only counts/summary, never raw uploaded rows).
- **Column-mapping — the "different column names we don't know of" problem (Jorge's versatility
  concern).** *Why:* a real tester's CSV will almost never use our exact nine J1939 header names, and
  a strict "reject unknown columns" gate makes "bring your own data" useless in practice. *Fix:* after
  parsing, present a **map-your-columns step** — for each expected signal in `FEATURE_COLUMNS`, a
  dropdown of the uploaded file's headers, **pre-filled by fuzzy auto-match** (case/spacing/synonym
  tolerant, e.g. `RPM`/`engine_rpm` → `engine_speed_rpm`) with a confidence hint. The tester confirms
  or corrects the mapping, then scores. Unmapped expected signals are allowed as **era-`NULL`** (the
  model handles missing signals natively — LightGBM), so a partial dataset still scores, honestly
  flagged as "N of 9 signals provided." Validation still **fails loud** on a file that has *no*
  plausible mapping (not numeric J1939-like data at all) — a clear 4xx, not a 500. This turns the
  upload from brittle to genuinely versatile, which is the point of the feature.
- **Honesty boundary (unchanged, load-bearing).** Predictions still come from the **`demo=fixture`
  model** — the page must say so on the upload result too (the tester's data is scored by the demo
  model, not the ≈0.82 full-data one). **Decision to make in F8:** whether to also serve the real
  full-data model for uploads — this trades image size / ADR-001 "never *report* off the fixture"
  against a more meaningful result; default is to keep serving the labelled demo and say so.
- **Guardrails to design first (forensic-watcher spirit).** Column/schema validation that **fails
  loud** with an actionable message (not a 500); a size/row cap; a content check that the upload is
  numeric J1939 signals, not arbitrary data; graceful handling of a malformed file (4xx, not crash).
- **DoD.** Upload a small valid CSV → per-row probabilities + summary render on `/demo`; a file with
  differently-named headers → the **map-your-columns step** auto-matches most and the tester confirms →
  scores; a partial dataset (some signals absent) → scores with the missing signals as era-`NULL`,
  honestly flagged "N of 9 provided"; a file that is *not* J1939-like → a clear 4xx (not a 500); an
  oversized upload → rejected within the cap; the demo-model banner is present on the result; offline
  tests (`[serve]`-gated) cover the round-trip, the fuzzy auto-match + manual override, the partial-data
  era-`NULL` path, the validation rejections, the size cap, and the no-raw-row-persistence posture.
  Update `docs/DEPLOY.md`/README with the upload + column-mapping capability.
- **Status.** ✅ **Done (2026-07-05, ADR-017).** New pure `src/pdm_mlops/upload.py` (parse with
  size/row caps → fuzzy `suggest_mapping` via stdlib `difflib` + a J1939 synonym table → `build_frame`
  with era-`NULL` for unmapped signals → fail-loud `assert_scorable` → `summarize`), wrapped by one
  two-mode `POST /demo/upload` (preview returns the suggested mapping; a confirmed mapping scores).
  `_score` refactored to a shared `_score_frame`. The `/demo` page gained a file-drop + a
  map-your-columns table + a scored-batch summary (histogram + first-rows table), still inline
  CSS/JS (no CDN). Fuzzy match hit **9/9** on realistically-renamed headers. **No raw uploaded row
  persisted** (a test asserts it). 25 offline tests green + the existing serve/demo suite unaffected.
  `python-multipart` added to `[serve]`. (Decision deferred as scoped: still serves the labelled
  `demo=fixture` model for uploads, not the full-data one.)

## F9 — Demo product-polish: friendly inputs + light/dark theme + i18n — **DONE (2026-07-06, ADR-018)**

**Why (observed, from Jorge's 2026-07-04 review):** the live `/demo` is **too technical** — it presents
nine raw J1939 signal fields (`engine_speed_rpm`, `coolant_temp_c`, …) with no hint of what a *reasonable*
value is, and it is **single-theme and English-only**. A curious tester with no heavy-equipment domain
knowledge can't tell a healthy engine from a failing one, and the bare probability output is unintuitive.
This is also a deliberate **front-end/product showcase** play (a strong-but-underexplored axis), and it is
**paired with the sibling `receivables-agent` Phase 9** (same theme + i18n work) so the two public demos
share a design language — do them close together.

- **9.1 — Friendly inputs (the biggest UX win).** *Why:* raw signal fields are meaningless to a
  non-expert. *Fix:* **preset example buttons** that fill plausible rows ("healthy engine", "failing
  bearing", "overheating") so a tester can try it in one click; **units + a short tooltip** per signal;
  **sane slider ranges** instead of free-text; and a **risk-meter / gauge** result (with a plain-language
  band — low/elevated/high risk) instead of a bare float. Keep the exact-value form available for power
  users.
- **9.2 — Light/dark theme.** Theme-aware (honours `prefers-color-scheme`) + a manual, persisted toggle.
  Note: `/demo` is currently **inline-CSS/JS, no CDN** (clean-room/offline-safe) — keep that constraint;
  the theming stays self-contained.
- **9.3 — Internationalization (i18n), EN + PT-BR at least.** A lightweight locale layer (a small string
  dictionary + toggle) over the UI chrome, the signal labels/tooltips, and the preset names. **Honesty
  note to preserve:** i18n covers the **UI shell**, not translation of the model/output semantics; the
  reported ≈0.82 framing and the `demo=fixture` banner stay intact in both languages.
- **DoD.** A domain-naive tester can get a sensible prediction in one click via a preset; each signal has
  a unit + tooltip + bounded input; the result shows a risk meter with a plain-language band; theme
  toggle works in both schemes and persists; UI renders in EN and PT-BR; the honesty banner + clean-room
  (no-CDN) constraint hold in both themes/languages. ADR-018 if a non-obvious choice is made. Coordinate
  the visual language with `receivables-agent` Phase 9.
- **Status.** **DONE (2026-07-06, ADR-018).** All three shipped: friendly inputs (`_SIGNAL_META`
  units/bounds/tooltips + one-click `_PRESETS` healthy/bearing/overheat + the risk-band meter),
  light/dark theme (CSS custom props, `prefers-color-scheme` + a persisted `data-theme` override),
  and EN/PT-BR i18n (`_DEMO_I18N` injected as JSON + an inline `t()` translator over the chrome,
  signal labels/tooltips, preset names). Self-contained, **no CDN**; the `demo=fixture` banner + the
  ≈0.82 framing hold in both languages (asserted by test). 5 new offline tests in `test_demo.py`;
  the existing demo tests stay green. Paired design language with `receivables-agent` Phase 9
  (ADR-015). Together F8 + F9 complete the "friendly, versatile, bring-your-own-data demo" vision.
