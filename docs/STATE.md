# State — forge-pdm-mlops

Updated: 2026-07-02 (notebook session: full green suite + F2.8 measured)

## Current focus

**Notebook session 2026-07-02 (verification/recording, not a new phase).** On the notebook
(`[ops]`+`[cloud]` installed, torch 2.12+cu130, RTX 4050): **(1)** full suite **133/137 green**
— all F4 serve, F7 store_pg/demo, F2.8 ceiling, F2.7 TCN pass, so the long-pending **F4
clean-run is recorded**; the **4 reds are all F5 drift** (`test_monitor`×2, `test_flows`×2) from
a real bug (below). **(2) F2.8 measured on full data** — the ceiling thesis is **refuted** (see
the F2.8 block). **Deferred to a next session (per Jorge):** fix the F5 drift bug, then the F7
live Cloud Run deploy (needs interactive `gcloud` auth + billing).

> **F5 drift bug (found on first-ever real `[ops]` run — these tests always skipped before).**
> `monitor.detect_drift` declares drift when the *share* of drifted features ≥
> `DRIFT_SHARE_THRESHOLD = 0.5`. The F5 test shifts **4** thermal signals; `FEATURE_COLUMNS`
> grew to **9** when `vibration_mms` was added in the 0.2.0 refresh, so share = 4/9 = **0.44 <
> 0.5** → `drifted=False`, but the tests assert `True` (worked at 4/8 = 0.5 before the feature
> was added). NOT an evidently-version issue — evidently 0.6.7 flags exactly the 4 shifted cols.
> Fix next session (ADR-013 policy call): lower the threshold, or make the synthetic shift
> mirror the real `season='heatwave'` breadth (≥5/9). A clean "tests that never run hide
> arithmetic" finding.

**F7 (Managed-cloud deploy — Cloud Run + Cloud SQL — the managed-cloud gate) — CODE +
SCAFFOLDING SHIPPED on the desktop (2026-07-02, ADR-015); the live Cloud Run URL + the
`[cloud]` green run land on the notebook.** This closes the one gate F0–F6 deliberately left
open: *operate a managed cloud runtime with a managed resource in production* — the senior
claim that "containerize an app" (F4/F6) is not. HF Spaces (F6) is free hosting; F7 runs the
**same** `Dockerfile.hf` image on **Google Cloud Run** (a managed, serverless container runtime,
not a VM) with **Cloud SQL for Postgres** as a **managed resource**.

- **`src/pdm_mlops/store_pg.py`** — the prediction log the managed DB exists for. `open_log(url)`
  → a `PredictionLog` (SQLAlchemy Core `append`/`recent` over the SAME code on Postgres in prod
  and tmp **SQLite** in tests) or **`None`** when `DATABASE_URL` is unset. **Graceful degrade is a
  hard invariant:** local `pdm serve`, the F6 HF Space, and CI all run **without** a DB and the
  demo simply doesn't persist — so adding the managed resource **cannot break** any existing
  deploy. **No PII by construction:** stores only the J1939 signal values (restricted to
  `FEATURE_COLUMNS` so a crafted key can't widen the row), the probability, the model version, a
  UTC timestamp — no user identity. A logging error is swallowed to a no-op (the model already
  answered; a missing row is the only cost). New `[cloud]` extra (`sqlalchemy` + `psycopg`),
  imported lazily so the package/core-CI never needs it.
- **The demo UI (`serve.py`)** — gives the managed DB an honest job. `GET /demo` is a
  self-contained (inline CSS/JS, **no CDN** — clean-room/offline-safe) "set the J1939 parameters →
  get the failure probability" page (the click-and-try pattern the receivables-agent showcase
  uses), carrying the **same `demo=fixture` honesty banner** as `/model-info` + the README.
  `POST /demo/predict` scores via the shared `_score` core **and** logs each row; the page reads
  the recent predictions back. `create_app(prediction_log=…)` is injectable (defaults to
  `open_log()`); a `DemoPredictResponse.persisted` flag labels the panel honestly.
- **`scripts/deploy_cloudrun.sh`** — the deploy: Artifact Registry repo + **Cloud Build** (no local
  Docker daemon) of `Dockerfile.hf` (already `$PORT`-aware via `hf_entrypoint.sh`) + Cloud SQL
  instance + **Secret Manager** (`DATABASE_URL`, password generated at deploy, never committed) +
  `gcloud run deploy` (`--add-cloudsql-instances`, `--set-secrets`, `--allow-unauthenticated`).
  **No secret in the repo** — only the parametrized script.
- **`docs/DEPLOY.md`** — a "Managed cloud: Cloud Run + Cloud SQL" section (one-time `gcloud`
  enablement, the deploy command, verify, tear-down) alongside the HF/Render/Fly.io targets.
- **Tests (13 new, green offline on the desktop):** `test_store_pg.py` (8, `[cloud]`-gated:
  round-trip, no-PII key restriction, era-NULL preserved, graceful-degrade on unset/bad URL,
  best-effort swallow on a backend error) + `test_demo.py` (5, `[serve]`+`[cloud]`-gated: demo
  round-trip **with and without** a log, the 503 contract, the honesty banner on the page,
  persistence + the recent-predictions panel). **Both extras are installed on this desktop, so all
  13 ran here (not skipped): 8 passed + 5 passed.** **ADR-015.**

  > **Runs here (desktop, 2026-07-02):** `pytest tests/test_store_pg.py` → **8 passed**;
  > `pytest tests/test_demo.py` → **5 passed** (4m10s — the fixture-training tax on the i3, not a
  > defect). **Lands on the notebook:** the actual `gcloud`/Cloud Build deploy (interactive auth +
  > the Docker path), the live Cloud Run URL + Cloud SQL instance, and folding these 13 into a full
  > green suite. Only then does the README get the `/demo` link and the career-system managed-cloud
  > gate flip — until the URL is live it is *in progress*, not closed.

**F6 (Hosted free-tier deploy — the live `/health` link) — DONE & LIVE (2026-07-02, ADR-014).
The production spine is now not just complete but REACHABLE — a clickable endpoint:
https://jorgeed-forge-pdm-mlops.hf.space/health** A fresh cloud deploy starts with an
empty registry, so a live `/health` would honestly say `model_loaded=false`. F6 ships a
**self-contained** image (`Dockerfile.hf`, distinct from the F4 mounted-volume `Dockerfile`) that
**bakes a demo registry at build time** so the endpoint serves a real prediction on boot:

- **`scripts/seed_demo_registry.py`** — `seed_registry(store_dir, seed=0)` trains on the committed
  smoke fixture → registers the winner → promotes it to `production` through the **same F3 gate**,
  into a **self-contained** SQLite store (DB + artifacts colocated via an explicit experiment
  `artifact_location`, so the image can carry it). Two load-bearing details: **(1)** it pins a
  **class-rich fixture seed (0)** — the default seed 42 lands a single-class fixture split →
  `DegenerateSplit` (an ADR-004 artifact; the full data is class-rich at any seed); **(2)** build
  path must equal run path (MLflow bakes **absolute** artifact URIs into the DB), so `Dockerfile.hf`
  fixes both at `/mlflow`.
- **The honesty boundary (ADR-014 / ADR-001 intact).** The baked model trains on the *fixture*,
  which ADR-001 forbids **reporting** — but ADR-001 forbids reporting, not **serving**. This is a
  **demo model for the live endpoint**, tagged `demo=fixture`, exposed by `/model-info`, and called
  a demo by the README + `docs/DEPLOY.md`. The real ≈0.82 model is the local `pdm train` one; that
  number is the only one ever quoted.
- **`docs/DEPLOY.md`** — the Hugging Face Spaces front-matter (`sdk: docker`,
  `dockerfile_path: Dockerfile.hf`, `app_port: 8000`), the push-to-Space steps, a local build
  smoke-test, and Render/Fly.io alternatives on the same image.
- **`retrain.yml`** — the F5 *placeholder* is gone; the scheduled workflow now runs `pdm flow` for
  real (installs `[ops,generate]`, uses the **real F3 gate**, so the cloud-scheduled loop can't
  auto-degrade either).
- **`test_seed_demo_registry.py` (4, offline, `[serve]`-gated):** promotes a **demo-tagged**
  version; a **fresh** serving process over the baked store reads `model_loaded=true` and predicts
  (the F6 DoD in miniature); the store is **self-contained**; the bake is **deterministic**.
  **ADR-014.**

  > **LIVE (2026-07-02): https://jorgeed-forge-pdm-mlops.hf.space/health → `{"status":"ok",
  > "model_loaded":true,"model_version":"1"}`.** Deployed to a Hugging Face Docker Space and
  > confirmed serving a real prediction. Getting it live surfaced **three container-only bugs a
  > native run hides** (all fixed; reusable HF-deploy lessons):
  >
  > 1. **HF ignores the front-matter `dockerfile_path`.** It built the default `Dockerfile` (the F4
  >    mounted-volume, no-bake image) instead of `Dockerfile.hf` — the build log stopped at
  >    `COPY data` with no bake, and `/health` was `model_loaded=false`. **Fix:** on the Space's
  >    `space-deploy` branch the self-contained bake image **is** the literal `Dockerfile` (and
  >    `dockerfile_path` dropped). GitHub `main` keeps the F4 `Dockerfile` + a separate `Dockerfile.hf`.
  > 2. **A pip-installed package resolves data files off `site-packages`, not the repo.**
  >    `config.SAMPLE_READINGS` = `Path(config.__file__).parents[2]/data/...` → in the container that
  >    is `/usr/local/lib/python3.12/data/...` (FileNotFoundError), because the package is installed,
  >    not run from the source tree. **Fix:** `seed_demo_registry.features_fixture()` resolves the
  >    fixture from the **script's** `../data`, not `config.REPO_ROOT`.
  > 3. **The demo bake belongs at startup, not build.** MLflow bakes **absolute** artifact paths into
  >    the DB, and HF only smudges the LFS fixture into a real file in the *running* container — so a
  >    build-time bake (as root, maybe on an un-smudged pointer) leaves a registry the runtime user
  >    can't serve. **Fix:** `scripts/hf_entrypoint.sh` bakes at container start (as `appuser`, at
  >    `/mlflow`, after smudge), `--skip-if-promoted` for idempotent warm restarts, fails loud on an
  >    un-smudged pointer. A `GET /` friendly index was added so the Space "App" tab isn't a 404, and
  >    `GIT_PYTHON_REFRESH=quiet` silences MLflow's harmless "no git" warning.
  >
  > **Space-branch mechanics:** the Space tracks a **`space-deploy`** branch (front-matter README +
  > LFS-tracked binaries [HF requires binaries via LFS] + the literal bake `Dockerfile`), kept off
  > `main` so the GitHub showcase stays LFS-free and its fixture a normal file. Update flow:
  > `git checkout space-deploy && git cherry-pick <main commits> && git push space space-deploy:main`
  > (`scripts/deploy_space.sh` automates it). The 4 F6 tests pass offline; the bake→serve cycle is
  > also verified live on HF.

**F5 (Drift monitoring + the auto-retrain loop — THE MARQUEE) — DONE (2026-07-02, on the
desktop). The production spine now runs end to end (ADR-013).** The closed loop the repo
exists to demonstrate: a distribution shift is detected, a fresh model is trained on the
shifted data, and it reaches production **only if it clears the same F3 gate** that guards
every promotion — so "auto-retrain" can never mean "auto-degrade". Two new modules:

- **`monitor.py`** — `drift_report(reference, current)` runs Evidently's `DataDriftPreset`
  over exactly the model's input signals (`features.FEATURE_COLUMNS`, via `select_features`
  — the monitored surface is the trained surface, leakage guard included) and distils it to a
  small, JSON-serialisable **`DriftReport`**. The decision is **ours**, not Evidently's
  default: drift is declared when the **share** of drifted features reaches
  `config.DRIFT_SHARE_THRESHOLD` (0.5) — a *share*, so one column tripping on noise doesn't
  fire a retrain; the loop reacts to a *distribution* shift (the `season` stimulus moves the
  thermal cluster together). `detect_drift(season=…)` is the high-level entry: baseline (no
  season) vs. the `season`-shifted window, both from the same canonical config.
- **`flows.py`** — `run_drift_retrain(...)` is a Prefect `@flow` of retried `@task`s:
  `detect_drift → [if drift] → retrain (F2 `train`) → promote-or-hold (F3 `promote`,
  **unchanged**)` → a structured **`FlowResult`**. The promote step is `registry.promote`
  verbatim, so a retrained candidate that doesn't beat the incumbent is **held**
  (`retrained=True, promoted=False`) — a normal governed outcome, not an error. Runs
  **in-process** on Prefect's local runner (no server); Prefect is imported **lazily inside**
  the call so importing the package / core CI never needs `[ops]`.
- **`pdm monitor` / `pdm flow`** go live — the **last two roadmap stubs**; `test_skeleton`
  now asserts the whole CLI surface is wired (no stub remains). `[ops]` extra capped
  **`evidently<0.7`** (0.7 rewrote the API `monitor.py` targets — same pin discipline as the
  generator, ADR-001).
- **`test_monitor.py` (6) + `test_flows.py` (5):** a synthetic multi-signal shift on the
  fixture stands in for the generator's `season` (offline); a real shift → drift, an identical
  frame → stable; the flow's drift branch fires and promotes; **a held candidate
  (`min_delta=-1.0`) proves the F3 gate still guards the automated path**; no-drift holds
  production. Both modules `importorskip` the `[ops]` libs, so core CI (only `[dev]`) skips
  them cleanly — exactly like `[serve]`/`[tune]`/`[deep]`. **121 tests total** (110 + 11).
  **ADR-013.**

  > **Verification note (2026-07-02):** built + wired on the i3 desktop, where `[ops]`
  > (Evidently + Prefect) is **not installed**, so `test_monitor`/`test_flows` **skip** here
  > and the rest of the offline suite stays green (the F5 modules import light — the lazy
  > `[ops]` imports are proven by a clean `from pdm_mlops import monitor, flows` without the
  > extra). **The 11 new F5 tests run for real on the notebook / CI where `[ops]` is
  > installed;** record the green there. (The notebook is separately finishing the pending F4
  > clean-110 run and the F2.8 GPU `pdm ceiling` numbers — unrelated to F5.)

**F4 (Serving — FastAPI over the promoted model) — DONE (2026-07-01). The second spine
build: the governed version F3 promotes is now served over HTTP (ADR-009).** New
`serve.py` — a FastAPI app that resolves the model through `models:/<name>@production`
(the same alias `registry` moves), so a promotion or rollback changes what `/predict`
answers **with no redeploy**:

- **`POST /predict`** — a batch of readings (the J1939 signals, era-NULL allowed as JSON
  `null`) → the per-row failure **probability** (positive class, the `models.Model`
  contract F2/F3 use). The frame is reindexed to the fixed `features.FEATURE_COLUMNS`
  order (JSON key order can't scramble it), missing → NaN, and `assert_no_leakage` re-runs.
- **`GET /health`** — 200 even with nothing promoted (`model_loaded=false`), so an
  orchestrator can tell "process up" from "ready to serve"; **`GET /model-info`** — the
  live production version + the metric it was gated on (auditable serving). Both 503 (not
  500) when nothing is promoted.
- **Probabilities via the native flavor, not pyfunc.** MLflow's generic pyfunc predict
  returns thresholded labels for our flavors; the product is the probability. The flavor
  is read from the model's own `MLmodel` metadata (`get_model_info().flavors`) — **not the
  `mlflow.log-model.history` run tag, which MLflow 3 no longer writes** (a real bug the
  first test caught: the fixture winner is lightgbm and a sklearn-flavor fallback load
  raised) — so a lightgbm or a logreg winner both serve correctly.
- **Lazy, cached load (`ModelStore`)** — the app starts before anything is promoted;
  clearing the cache re-resolves the alias, which is exactly picking up a rollback.
- **`Dockerfile` + `docker-compose.yml`** (serving + the MLflow UI on one shared registry
  volume). `config.default_tracking_uri()` honours `MLFLOW_TRACKING_URI`, so the container
  serves the training host's registry with nothing baked in. `pdm serve --host/--port`.
- **`test_serve.py` (10, offline, tmp SQLite):** the DoD **prediction round-trip** on a
  promoted fixture-trained model, health/model-info, the era-NULL passthrough + column
  reorder, the 503-without-a-model paths, and a rollback picked up by a fresh store load.
  **110 tests total** (100 + 10). **ADR-009.**

  > **Verification note (2026-07-01):** on the low-end i3 desktop the last full run reached
  > **79 tests with zero failures** before the machine wedged on a heavy-training test — the
  > known CPU-TCN / repeated-training stall ([[resources_compute]]), *not* a code defect. The
  > F4 no-global-state load fix was independently confirmed end-to-end by a **two-iteration
  > train→serve→train repro** ("PASS both isolated" — a lightgbm *and* a logreg winner each
  > served correctly, no cross-registration leak). **The clean full 110-green `pytest` run is
  > pending on the notebook (GPU/faster CPU);** re-run there to record it.

**F3 (Registry + gated promotion + rollback) — DONE (2026-07-01). The first build on the
MLOps spine the repo exists to close (ADR-008).** Governed model lifecycle on the same MLflow
SQLite registry F2 already writes to: a `production` **alias** (MLflow 3 deprecated the classic
stages, so promotion moves an alias, not a stage) that promotion re-points **only when a
candidate clears the eval-metric gate**, and a **rollback** that restores the version it
superseded. New `registry.py`:

- **`promote(client, name, version, *, gate, min_delta)`** — reads the candidate's and the
  incumbent's ROC-AUC from their **source runs** (the metric `train` logged before registering),
  and moves the `production` alias only if `candidate >= incumbent - min_delta`. **A strictly
  worse candidate does NOT promote** (the DoD) — and that rejection is a *governed outcome*, a
  structured `PromotionResult(promoted=False, …)`, **not** an exception. `min_delta=0.0` default
  (ties promote — newer wins on equal evidence); first-ever promotion always passes (nothing to
  protect yet); `gate=False` is the `--force` escape hatch. Only a malformed *request* (unknown
  version / a run with no logged metric) raises `PromotionError`.
- **`rollback(client, name)`** — restores the prior production version. Deterministic via a
  `superseded_production_version` **tag** written on each promoted version (no run-history
  scraping); raises if nothing is promoted or the current version was the first (no predecessor).
- **`production_version` / `version_metric` / `latest_version` / `format_promotion`** round it
  out. All versions normalised to **str** at the boundary (MLflow returns int on some paths,
  str on others — a real inconsistency the surface hides).
- **`pdm promote` (`--version`/`--min-delta`/`--force`) + `pdm rollback`** live; `pdm promote`
  exits non-zero on a gate rejection so a CI/script step notices.
- **`test_registry.py` (14, offline, tmp SQLite):** the two DoD assertions (worse candidate does
  not promote; rollback restores the prior version) + first-promotion / tie / `min_delta`
  tolerance / `--force` bypass / loud errors on malformed input / `latest_version` / alias-unset.
  **100 tests green offline** (86 + 14). **ADR-008.**

**F2.8 (Characterize the ceiling — is the limit the data or the model?) — MEASURED on the full
data (GPU notebook, 2026-07-02, seed 42). The capstone that closes the F2.* arc — and it
HONESTLY REFUTES its own thesis.** The honest close of the F2.5→F2.7 investigation: stop
*asserting* "0.82 is the data's information limit" and **measure** it. New `ceiling.py`, three
instruments, all on the **exact F1 unit split / seed / test rows** and all **label-honest**
(labels read only to grade/bound, never as an honest feature — the ADR-003 / `detect_score`
discipline). **Measured full-data result:**

| instrument | result |
|---|---|
| overall (per-row honest) | **0.8125** |
| decomposition by TTF horizon | [0,6) h **0.9183** · [6,24) **0.9290** · [24,72) **0.9264** · [72,168) **0.7162** · [168,∞) 0.5555 (pos=17) |
| decomposition by failure mode | bearing **0.8732** · oil_starve **0.7959** · overheat **0.7720** |
| upper-bound (FENCED, leaks failure_mode + TTF) | 1.0000 → gap over honest **+0.1875** |
| stacking probe — two-rung (GBDT) | best base temporal 0.8194 → **stack 0.8267 (+0.0073)** |
| stacking probe — three-rung (+TCN) | TCN 0.7979 → **stack 0.8257 (+0.0063)** |

**VERDICT (robust across two-/three-rung): `ceiling_is_data = False`.** The stack beats its best
base member by ~+0.006–0.007 → the rungs are **NOT fully redundant** → the "0.82 is purely the
data's ceiling" thesis is **not confirmed**. The instrument built to confirm the ceiling-is-data
hypothesis partially refuted it: a modest but consistent combinable signal remains (magnitude on
par with the +0.0069 F2.7 called "temporal helps" — so calling *this* noise would be inconsistent).
Honest caveat: a single deterministic OOF LogisticRegression meta-learner, no confidence interval;
it is a **probe, not a product** (never shipped as the model). The decomposition shape is exactly
as theorized — sharp near failure (~0.92–0.93 within 72 h), fading far out (0.72 at [72,168), and
the [168,∞) band is healthy-and-unpredictable by construction, only 17 positives).

The three instruments (all on the exact F1 split, all label-honest):

1. **Decomposition** (`decompose`) — the honest per-row held-out AUC sliced by **time-to-failure
   horizon** (`[0,6)…[168,∞) h`) and by **failure mode**; each band's positives vs. all healthy
   rows, so the *shape* of predictability is visible (expected high near failure, fading far
   out — most of the 168 h window is healthy-and-unpredictable by construction). Time-to-failure
   is derived label-side (`time_to_failure`: event = one stride past a unit's last positive) and
   used only to bucket.
2. **Label-leaking upper-bound** (`upper_bound`) — a LightGBM that also sees `failure_mode` +
   the derived `time_to_failure_h`, bounding the **irreducible** error. A **fenced diagnostic**:
   own field, never a reported metric; the honest frames are asserted leak-free by
   `_assert_honest_frame` (a `LEAK_FEATURES` guard *on top of* `assert_no_leakage`, so even the
   non-target `time_to_failure_h` can't reach the honest path); the fence is asserted by test.
3. **Stacking redundancy probe** (`stacking_probe`) — an **OOF unit-grouped** (`GroupKFold`)
   LogisticRegression meta-learner over the base rungs. If it can't beat its best base member ⇒
   rungs are information-redundant ⇒ ceiling is the data; **measured, it DID beat the best base**
   (+0.0073 two-rung / +0.0063 three-rung) ⇒ rungs are **not** fully redundant. A probe, **not a
   product** — reported honestly either way.

**Compute / the TCN seam — folded in (2026-07-02).** The two base rungs are the cheap F2.7
LightGBM frames (CPU). The F2.7 **TCN** OOF was produced on the notebook GPU (5-fold unit-grouped
GroupKFold matching `_oof_predictions`, + a full-train fit for the test column) and folded through
`extra_oof={name: (oof_train, proba_test)}` **without a rewrite** (alignment asserted, 0 NaN OOF
rows). The three-rung verdict matches the two-rung one (TCN is the weakest rung, 0.7979 < temporal
0.8194, so it *slightly lowers* the stack, 0.8267→0.8257, but the margin stays positive). **TCN
cross-version determinism note:** the same-geometry TCN (32 ch · 4 layers · window 24 · 12 epochs)
reproduces at **0.7979** on this notebook (torch 2.12.0+cu130), vs the **0.8148** ADR-007 recorded
on the earlier build — a *cross-CUDA-version* numerics gap (`use_deterministic_algorithms` pins
within-version reproducibility, not across cuDNN/CUDA builds; more epochs, 8→12, did **not** close
it, so it is not under-training). It does not affect the verdict. `pdm ceiling` runs the two-rung
version live; new `ceiling.py` + **13 tests** (offline, deterministic). **ADR-010.**

**F2.7 (Temporal modelling — does the trajectory help?) — DONE.** The honest follow-on to
the F2.6 HPO-null finding: if tuning is exhausted because the ceiling is a *representation*
limit (the failure is a degradation **ramp** — generator ADR-020 — and a per-row model
discards the trajectory), then the lever is temporal structure, **measured to earn its
place**. A **three-rung ladder** in new `sequence.py`, all on the **same unit split / seed /
metric / test rows** as F1 (proven row-identical by test). **Measured on the full data (GPU
RTX 4050, seed 42, deterministic across re-runs):**

| rung | ROC-AUC | Δ |
|---|---|---|
| (a) per-row LightGBM (the bar) | 0.8125 | — |
| (b) temporal-features LightGBM | **0.8194** | **+0.0069** vs. (a) |
| (c) dilated causal TCN | 0.8148 | **−0.0046** vs. (b) |

Two findings, both kept: **(1) temporal structure helps** (+0.0069 over per-row — the
trajectory carries signal, confirming the representation thesis), but the lift is modest
because the ramp is gentle; **(2) the deep model does NOT earn its place** — the TCN lands
below the cheap, interpretable temporal-features LightGBM, so rung (b) wins. The cheap rung
exists precisely to stop us conflating "temporal helps" with "deep helps", and it did its job
(the F2.5 autoencoder discipline again). The TCN geometry was fixed **a priori** and never
tuned against the reported test rows (that would be the very leakage the repo guards).
**HPO follow-up (measured, settles "could tuning the TCN win?"):** a seeded Optuna study (12
trials) over the TCN geometry, scored by **unit-grouped 3-fold CV on the *training* split
only** (test rows never seen), then evaluated once on the same held-out rows → tuned TCN
**0.8107**, *below* the a-priori TCN (−0.0041) and the temporal bar (−0.0087). **Tuning does
not rescue the deep model** — it sits within noise of the ≈0.82 data ceiling; the cheap rung
(b) keeps the title (doubly confirms the F2.6 HPO-null). The
causal-convolution no-future-leakage is **structural**; era-NULL enters as impute + a
missingness-mask channel; every test row scored (short histories left-padded). `pdm sequence`
runs it live; logged to the same MLflow experiment, winner registrable. New `sequence.py` +
10 tests. **73 tests green offline.** ADR-007.

**Data realism refresh — generator `can-telemetry-forge` 0.1.0 → 0.2.0 (cross-repo).**
Consuming the generator exposed that its failures had **no temporal signature** (a
failing unit's pre-failure rows were identical to its healthy rows), so a per-row model
scored ≈ 0.55 *by construction* — not for lack of modelling effort here. Fixed **in the
generator** (its ADR-020: a progressive pre-failure degradation ramp), not by tuning the
model against the data. Two changes landed on this side: (1) the `[generate]` pin moved
to `==0.2.0` and the committed smoke fixture was **rebuilt** against it; (2) `vibration_mms`
(the bearing signature, era-gated, previously unused) was added to `features.FEATURE_COLUMNS`
— it also flows into `detect.SIGNAL_COLUMNS` automatically. **Measured on a regenerated
dataset: ≈ 0.55 → 0.73 (ramp alone) → ≈ 0.82 (with vibration).** Two generator hazard
rebalances were prototyped, scored, and **rejected** (neither beat the ramp; logged in the
generator's ADR-020). The earlier "score is faint by design / ~0.55" framing in F2/F2.5/F2.6
is now **superseded** — the score is a real ≈ 0.82, earned from raw sensors with the
leakage guards intact. (F2–F2.6 process/instrumentation claims are unchanged and now sit on
honestly-learnable data.) Re-run `pdm train` to refresh the registered metrics.

**F2.6 (Tune + diagnose — instrumentation) — DONE.** On the F2.5-**cleaned** inputs
(`features.prepare(suspect_feature=True)`), "why this model, with these params" is now
**visible, tracked, and guarded** — deliberately *not* an accuracy play, now **measured**:
on the refreshed 0.2.0 data HPO moves the held-out test AUC by **+0.003 (lightgbm) / 0.000
(logreg)** — the real ≈0.82 came from the data (ADR-020), not the tuning (ADR-006). Three
pieces:

- **HPO (`tune.py`)** — one seeded **Optuna** study per contender over a *restricted,
  declared* tunable space (`models.LOGREG_TUNABLE`/`LIGHTGBM_TUNABLE`), scored by
  **unit-grouped `GroupKFold`** on the *training* split only, so the search can't leak a
  unit across folds (ADR-003 holds) and never tunes against the reported test number.
  Tracked to a `-tune` MLflow experiment; `pdm tune` runs it; tuned params feed
  `train(tuned=…)`. New `[tune]` extra (`optuna` + optional `matplotlib`).
- **Diagnostics (`diagnostics.log_diagnostics`)** — per fitted model, **artifacts** on its
  MLflow run: feature importance (`signal_suspect` ranks), calibration, a precision/recall
  threshold sweep, a learning curve. **CSV always** (CI-light, reproducible) + **PNG when
  matplotlib is present** — matplotlib is an optional nicety, never a hard dep.
- **Training watchers (`diagnostics.audit_fit`, opt-in `--audit`)** — the forensic-watcher
  pattern: an **overfit-gap** guard (train − grouped-CV AUC > 0.15) and a
  **majority-baseline** guard (test AUC must beat 0.5). Raise `FitAudit` in strict mode.
  `DegenerateSplit` (F2) is the third in this family.

**An honest finding, kept (not hidden).** On the **15-unit smoke fixture** the deep
LightGBM legitimately overfits — train AUC ≈ 1.0 vs. grouped-CV ≈ 0.60 → the overfit-gap
watcher **trips**. That is the watcher *earning its keep*, not a bug: grouped CV on so few
units is pessimistic by construction (the same fixture-size artifact ADR-004 records for
`DegenerateSplit`). A dedicated test asserts the trip on the fixture; the watcher's *pass*
path is tested on a shallow/regularised fit; the honest setting is the full 100-unit
dataset. **`pdm train --tune --audit --diagnose` chains all three live.** **63 tests green
offline** (49 from F0–F2.5 + 14 new). ADR-006.

**F2.5 (Outlier robustness — clean first) — DONE.** A three-rung detection ladder
(`detect.py`) — multivariate (IsolationForest + robust Mahalanobis), temporal
(exact-value freeze runs + sustained monotone creep, with the detectable signals chosen
**unsupervised** at fit time), and a CPU-only PyTorch autoencoder — each **scored against
the generator's ground-truth labels** (`detect_score.py`), which are read in exactly one
place to *grade* (and to tune the temporal constants), never as a detector input or a
model feature (the ADR-003 leakage guard holds, asserted by test). The autoencoder
**earns its place** (best overall, beats the cheap rungs on subtle recall). The temporal
rung is a **deliberate rewrite**: its first rolling-variance/slope form scored ~0.02 F1
and flagged >85 % of rows; the diagnosis became the fix, and the negative result is
logged in ADR-005. Output = a leakage-safe **`signal_suspect`** feature
(`suspect.add_signal_suspect`, opt-in via `features.prepare(suspect_feature=True)`) + a
**data-quality watcher** (`suspect.data_quality_check`, forensic-watcher pattern, doubles
as an F5 drift signal). `pdm detect` prints the scored table. New `[deep]` torch extra,
out of core CI. **49 tests green offline** (27 from F0–F2 + 22 new). ADR-005.

**F2 (Train + track — MVP core) — DONE.** `pdm train` now runs an honest
**two-model comparison** (LogReg pipeline + LightGBM behind one interface), logs
**both** as MLflow runs (params + ROC-AUC + the fitted model artifact), picks the
winner by the primary metric, and **registers** it in the MLflow Model Registry —
all on a server-free local **SQLite** backend (ADR-004). Deterministic: same seed →
same metrics. **27 tests green offline** (15 from F0/F1 + 12 new). This is the MVP
core; F3 builds gated promotion/rollback on the same registry.

**F1 (Data + features) — DONE.** The data layer regenerates the **full** dataset
from the canonical config when the generator is present, and falls back **loudly**
to the committed smoke fixture offline (ADR-001). The feature layer turns `readings`
into a leakage-safe modelling frame: signals-only inputs with a tested leakage
guard, era-NULL missingness preserved (no imputation), and a deterministic
**unit-grouped** train/test split. ADR-003 records the three guards. (F0 — the
runnable skeleton — closed at an earlier boundary.)

## Done

- **Package skeleton** (`src/pdm_mlops/`): `__init__` (version only), `config.py`
  (paths, MLflow wiring, seeds, thresholds — one source of truth), `cli.py` (`pdm`
  with `--version` + `train`/`serve`/`flow`/`monitor` stubbed to point at their
  phase). `pyproject.toml` with core deps (numpy/pandas/pyarrow/scikit-learn/
  lightgbm/mlflow) + `dev`/`serve`/`ops`/`generate` extras; the `[generate]` extra
  pins `can-telemetry-forge==0.1.0`.
- **Canonical dataset config** (`configs/dataset.json`) — the single cross-machine
  source of truth (seed 42 / 90 days / 5min / 168h horizon). The full dataset
  regenerates identically on any machine from this + the pinned generator (ADR-001).
- **Offline smoke fixture** (`data/sample_readings.parquet`, ~185 KB) built by
  `scripts/build_sample.py` as a *strict reduction of the same canonical config*
  (14-day window, ~hourly stride, stratified 20-unit subsample, modelling columns,
  float32 + categoricals, zstd). 6,720 rows × 14 cols, 5.8% failure rate. **It is a
  smoke fixture only — never a training set** (ADR-001).
- **CI** (`.github/workflows/ci.yml`): Linux+Windows × 3.11/3.12, installs `[dev]`
  only, smoke-tests `pdm --version`, runs offline pytest. **`retrain.yml`**: the
  scheduled (cron) + manual cloud trigger surface for the F5 Prefect flow (a
  marked placeholder until F5).
- **Tests** (`tests/test_skeleton.py`): import, CLI help/stubs, fixture presence.
- **Docs**: CLAUDE.md, ROADMAP (F0–F6), ARCHITECTURE, DECISIONS (ADR-001/002),
  this STATE. `.gitignore` (mlruns, generated data, reports; un-ignores the fixture).
- **GitHub repo metadata applied** to `JorgeEd13/forge-pdm-mlops` (description + 14
  topics live in the About sidebar); command recorded in `.github/REPO_META.md`.
  The repo exists on GitHub with git initialized (branch `main`, remote set, no
  commits yet — F0 is the first commit).

### F1 — Data + features (2026-06-26)

- **`data.py`** — `load_readings()` prefers **full regeneration** from
  `configs/dataset.json` via the pinned generator (`regenerate_full`, with a
  `season` override hook for the F5 drift loop); falls back to the committed fixture
  with a **loud** `UserWarning` when the generator is absent, or raises
  `GeneratorUnavailable` if fallback is disabled. Verified on the real path: 90-day
  full dataset = **3.47M rows × 134 units** in ~98 s.
- **`features.py`** — `prepare()` returns a frozen `Dataset` (X/y/groups train+test).
  Inputs are the 8 J1939 signals only; `assert_no_leakage` (tested to fire) blocks
  the target + `failure_mode`/`anomaly_type`/`is_outlier`. **Era-NULL preserved** (no
  imputation). **Unit-grouped** `GroupShuffleSplit` (seeded, 25% of units), asserted
  disjoint — full data splits 100/34 units, fixture splits its 20 units cleanly.
- **Tests** — `test_data.py` (5: fixture load, fallback warns, season flagged,
  no-fallback raises, full-path-not-touching-fixture via monkeypatch) +
  `test_features.py` (6: signals-only/leakage-fires, unit-disjoint, determinism,
  seed-drives-partition, era-NULL preserved, binary target). All offline.
- **ADR-003** records the leakage guard / era-NULL / unit-split policy.

### F2 — Train + track (2026-06-26)

- **`models.py`** — two contenders behind one `Model` (`fit`/`predict_proba`):
  `build_logreg` (median-impute → scale → `LogisticRegression`, `class_weight=
  balanced`; imputation lives **in the pipeline** so era-NULL stays intact upstream)
  and `build_lightgbm` (`LGBMClassifier`, native NaN, no scaling). `build_all` returns
  them in fixed order. Each carries a flat `params` dict for MLflow.
- **`train.py`** — `train()` loops `build_all`, opens an **MLflow run** per model
  (params + ROC-AUC + the model artifact), picks the best `roc_auc`, and **registers**
  the winner. Local **SQLite** tracking/registry (`config.sqlite_tracking_uri`),
  injectable data source (`readings=`/`load=`) so tests stay offline. `_score` raises
  **`DegenerateSplit`** if the test set is single-class (a fixture-only artifact — the
  full split is class-rich) instead of logging a meaningless `nan`.
- **`pdm train`** — wired live (`--seed`, `--no-register`); prints a comparison table.
- **ADR-004** — two-model comparison; SQLite backend (MLflow 3 retired the bare file
  store); sklearn artifacts via cloudpickle (skops rejects `numpy.dtype`); the
  `DegenerateSplit` guard.
- **Tests** — `test_models.py` (7: both fit & emit proba, one interface, era-NULL fed
  in raw, LightGBM eats NaN, LogReg pipeline imputes, same-seed-same-fit) +
  `test_train.py` (5: two tracked runs + registered winner, `--no-register` skips the
  registry, same-seed-same-metric, defaults to the data loader, `DegenerateSplit`
  fires on the degenerate fixture seed). MLflow → tmp SQLite; all offline.

### F2.5 — Outlier robustness (2026-06-26)

- **`detect.py`** — the ladder behind one `Detector` surface (`fit`/`score` → a
  `[0,1]` suspicion per row, **label-free**): `MultivariateDetector` (IsolationForest +
  `MinCovDet` Mahalanobis, `support_fraction=0.9`), `TemporalDetector` (exact-value
  freeze runs ≥ `STUCK_MIN_RUN` on *unsupervised-selected* continuous signals + a
  sustained monotone-creep window on *non-monotone* signals), `AutoencoderDetector`
  (CPU torch, reconstruction error). `build_ladder`/`fit_score_all`.
- **`detect_score.py`** — reads the labels (the **only** place) to grade each rung:
  ROC-AUC + AP vs. `is_outlier`, per-`anomaly_type` recall at a **tie-aware** top-2%
  alarm budget (`_alarm_set` — a sparse detector can't win by flagging everything), and
  the **autoencoder-earns-its-place** verdict. `pdm detect` prints the table.
- **`suspect.py`** — `add_signal_suspect` (combines rungs → the leakage-safe
  `signal_suspect` column, re-passes the guard) wired into `features.prepare(
  suspect_feature=True)`; `data_quality_check` forensic watcher (raises
  `DataQualitySpike` in strict mode; F5 drift signal).
- **The temporal rewrite (ADR-005).** Rolling-variance/slope → stuck/drift scored ~0.02
  F1 and flagged >85% of rows; rewritten to the freeze-run / monotone-creep signatures
  with unsupervised signal eligibility → stuck recall ≈0.59 @ precision ≈0.64 on
  native-res, drift high-precision/low-recall. The negative result is documented, not
  hidden. Thresholds auto-derived vs. ground truth (with Jorge) then pinned.
- **`[deep]` extra** (CPU `torch`), kept out of core CI. **ADR-005.**
- **Tests** — `test_detect.py` (12: ranges, label-free proof, determinism, unsupervised
  signal selection, no-flag-everything, `_equal_run_lengths`, AE skip-if-no-torch),
  `test_detect_score.py` (6: grades well-formed, tie-aware budget dense+sparse, NaN for
  absent family, loud on missing labels), `test_suspect.py` (6: feature leakage-safe,
  `prepare` wiring, determinism, watcher fires + strict raises). **49 total green.**

### F2.6 — Tune + diagnose (2026-06-26)

- **`tune.py`** — `tune_model` runs a seeded **Optuna** study per contender over its
  restricted tunable space, objective = mean ROC-AUC over **unit-grouped `GroupKFold`**
  folds of the *training* split (single-class folds skipped; all-degenerate → `nan` →
  pruned). `tune` prepares the **cleaned** frame (`suspect_feature=True`), runs both
  studies, logs each to a `-tune` MLflow experiment, returns `{name: TuneResult}`.
  `DEFAULT_TRIALS=40`. The test split is never seen by the search.
- **`models.py`** — `build_logreg`/`build_lightgbm` take validated `overrides`
  (`_check_overrides` raises on an unknown key); with none they are the F2 baseline
  exactly. `BUILDERS` (name→builder) + `build_all(tuned=…)` feed tuned params by name.
- **`diagnostics.py`** — `log_diagnostics` writes feature-importance / calibration /
  threshold-sweep / learning-curve **artifacts** (CSV always + PNG if matplotlib) to the
  active run. `audit_fit` = overfit-gap (`OVERFIT_GAP_LIMIT=0.15`) + majority-baseline
  (`MAJORITY_AUC=0.5`) watchers → `AuditReport`, raise `FitAudit` in strict mode.
- **`train.py`** — gained `tuned=` / `clean=` (cleaned frame; defaults on when `tuned`
  given) / `audit=` (strict watchers) / `diagnose=` (artifacts); logs `cleaned_inputs`,
  `tuned`, and the audit's `cv_roc_auc`/`train_roc_auc`.
- **`cli.py`** — `pdm tune` (`--seed`, `--trials`) live; `pdm train` gained `--tune`
  (search then train tuned+cleaned), `--audit`, `--diagnose`, `--clean`.
- **`[tune]` extra** (`optuna` + optional `matplotlib`), out of core CI. **ADR-006.**
- **Tests** — `test_tune.py` (6: grouped CV never shares a unit, only-tunable params,
  tuned params build a model, determinism, one tracked run/model, searches the cleaned
  frame), `test_diagnostics.py` (7: importance ranks `signal_suspect`, artifacts land,
  watcher pass-path on a shallow fit, **fixture deep-fit trips overfit by design**,
  majority watcher fires + strict raises), + 2 in `test_train.py` (tuned params thread
  through on the cleaned frame; `--audit` raises). **63 total green.**

### F2.7 — Temporal modelling (2026-06-27)

- **`sequence.py`** — the three-rung ladder behind one comparison. `split_indices` mirrors
  `features.prepare`'s `GroupShuffleSplit` **bit-for-bit** (proven row-identical by test), so
  all rungs score on the *same* held-out rows. `temporal_features` builds per-unit **causal**
  rolling mean/std/slope/delta (groupby-rolling resets at each unit boundary → no future
  leak, no cross-unit bleed; re-passes `assert_no_leakage`). `build_windows` standardises on
  **train rows only**, imputes era-NULL to the train mean **and** emits a was-present mask
  channel, and pre-computes left-padded causal window indices (memory-bounded gather, not a
  materialised N×W×C tensor). `TCNClassifier` = a dilated **causal** 1-D conv stack
  (left-pad + right-chomp ⇒ the no-peek property is *structural*) → last-timestep head →
  per-row proba; `fit(readings, train_idx, y)` / `predict_proba(readings, idx)`. `compare`
  runs (a) per-row LightGBM, (b) temporal-features LightGBM, (c) the TCN, logs each to the
  **same** MLflow experiment, returns the three-way result + the earns-its-place verdict +
  registrable winner.
- **`cli.py`** — `pdm sequence` (`--window`, `--epochs`, `--channels`, `--device`,
  `--register`) live; the real path regenerates the full dataset, the TCN auto-selects CUDA.
- **Measured (full data, GPU RTX 4050, seed 42, deterministic across two re-runs):** per-row
  **0.8125** → temporal-features **0.8194** (**+0.0069**, temporal *does* help) → TCN
  **0.8148** (**−0.0046** vs. (b), deep does **not** earn its place). Cheap interpretable
  rung wins; verdict reported either way. TCN geometry fixed a-priori, **never** tuned vs. the
  test rows (leakage guard). **ADR-007** carries the table + the two honest findings.
- **`[deep]` torch extra reused** (no new dependency), out of core CI.
- **Tests** — `test_sequence.py` (10: split row-identical to F1, unit-disjoint, temporal
  features leakage-safe + **no future peek**, windows causal/unit-bounded, left-pad zeroed,
  TCN deterministic + scores every test row, three-rung compare on the same rows + registers,
  compare determinism). Torch-free rungs always run; TCN rungs skip without `[deep]`.
  **73 total green offline.**

### F2.8 — Characterize the ceiling (2026-07-01)

- **`ceiling.py`** — `characterize` runs three instruments on the shared honest base
  (`build_base`: the F2.7 per-row + temporal-features LightGBM frames on the **exact F1**
  unit split): `decompose` (held-out AUC by TTF-horizon bucket + by failure mode),
  `upper_bound` (a **fenced** label-leaking LightGBM diagnostic — `failure_mode` one-hot +
  derived `time_to_failure_h`), `stacking_probe` (OOF unit-grouped LogisticRegression over the
  rungs → beats-best-base verdict). `time_to_failure` derives the label-side TTF (event = one
  stride past a unit's last positive). `format_report` prints the capstone; `CeilingReport.
  ceiling_is_data` = the thesis flag (¬stack-beats-best-base).
- **The fence.** `_assert_honest_frame` = `features.assert_no_leakage` **plus** a
  `LEAK_FEATURES` guard, so even the non-target `time_to_failure_h` can never reach the honest
  path; the leaky frame is built only inside `upper_bound` and returned in its own field.
- **The TCN seam.** `stacking_probe(extra_oof={name: (oof_train, proba_test)})` folds a
  GPU-produced TCN OOF column in with no rewrite (alignment asserted). CPU-only otherwise.
- **`pdm ceiling`** (`--seed`, `--window`) live. No new dependency.
- **Tests** — `test_ceiling.py` (13: ttf finite exactly on positives + within horizon, base
  frames leak-free + exact-F1-split + unit-disjoint, decomposition covers horizon+modes with
  positives summing to held-out positives, upper-bound bounds & is fenced — **the fence fires**
  on `time_to_failure_h` and on target/label columns — determinism, the stacking seam folds in
  / rejects misaligned extra OOF). Offline, deterministic. **86 total green offline** (73 +
  13). **ADR-010.**
- **MEASURED (GPU notebook, 2026-07-02, seed 42, 3.47M rows).** `pdm ceiling` (two-rung) +
  a TCN-OOF fold via the `extra_oof` seam. Overall per-row **0.8125**; decomposition sharp
  near failure (0.92–0.93 within 72 h) fading far out; upper-bound (fenced) 1.0000, gap
  **+0.1875**. **Stacking probe: stack 0.8267 (+0.0073) two-rung / 0.8257 (+0.0063) three-rung
  → `ceiling_is_data = False`.** The thesis is **refuted**: the rungs are not fully redundant,
  ~+0.006–0.007 combinable signal remains (probe, not product). TCN reproduces at 0.7979 here
  (torch 2.12+cu130) vs ADR-007's 0.8148 — a cross-CUDA-version numerics gap, not under-training
  (8→12 epochs did not close it); it is the weakest rung and does not change the verdict. See
  the F2.8 "Current focus" block for the full table.

### F3 — Registry + gated promotion + rollback (2026-07-01)

- **`registry.py`** — governance on the F2 registry. `promote` (metric-gated `production`-alias
  move, structured `PromotionResult`), `rollback` (tag-recorded predecessor restore),
  `production_version` / `version_metric` / `latest_version` / `format_promotion`. **Aliases,
  not the MLflow-3-deprecated stages** (ADR-008). A rejection is a governed *outcome*, not an
  exception; malformed requests raise `PromotionError`. Versions normalised to str at the
  boundary.
- **`cli.py`** — `pdm promote` (`--version` defaults to the latest registered, `--min-delta`,
  `--force`) + `pdm rollback` live; `pdm promote` exits 1 on a gate rejection.
- **Tests** — `test_registry.py` (14: **worse candidate does not promote** + **rollback restores
  the prior version** (the DoD), better-promotes, first-promotion-no-incumbent, tie-promotes,
  `min_delta` tolerates a small regression, `--force` bypass, rollback-without-predecessor /
  nothing-promoted raise, unknown-version / no-metric raise, `latest_version`, alias-unset →
  None). MLflow → tmp SQLite; all offline. **100 total green offline** (86 + 14). **ADR-008.**

### F4 — Serving (2026-07-01)

- **`serve.py`** — `create_app()` (FastAPI) + `ModelStore` (lazy, cached resolution of the
  `production` alias). `/predict` (readings → per-row failure probability), `/health` (200
  even with no model, `model_loaded` flag), `/model-info` (live version + gated metric).
  `_load_predict_proba` **touches no global MLflow state**: it resolves the alias to a version
  via the injected client, `download_artifacts` to a local path, and loads that — because a
  `models:/@alias` load pins MLflow 3's process-global registry URI (`set_registry_uri(None)`
  does *not* un-pin it) and would redirect a co-resident `train`'s `register_model` (a real
  leak the multi-test run caught). Flavor read from the model's own `MLmodel` metadata
  (`get_model_info().flavors`, not the MLflow-3-dropped run-history tag); returns the
  positive-class column. `_to_frame` reindexes to `FEATURE_COLUMNS`, coerces to numeric
  (era-NULL → NaN), re-runs `assert_no_leakage`. 503 (not 500) when nothing is promoted.
- **`config.default_tracking_uri()`** — honours `MLFLOW_TRACKING_URI` (the container path)
  else the local `mlruns/` SQLite; `registry._client()` routes through it.
- **`cli.py`** — `pdm serve --host/--port` runs uvicorn on the app.
- **`Dockerfile`** (slim, `[serve]` extra, libgomp for LightGBM) + **`docker-compose.yml`**
  (serving + MLflow UI on one shared registry volume, nothing baked in).
- **Tests** — `test_serve.py` (11: predict round-trip on a promoted fixture-trained model
  (the DoD), era-NULL passthrough, column reorder, health-loaded/-empty, model-info,
  predict/model-info 503 without a model, rollback picked up by a fresh store load, empty
  batch 422). Offline, tmp SQLite. **110 total green offline** (100 + 10). **ADR-009.**

### F5 — Drift monitoring + the auto-retrain loop (2026-07-02)

- **`monitor.py`** — `drift_report` runs Evidently `DataDriftPreset` over `FEATURE_COLUMNS`
  (via `select_features`) → a distilled `DriftReport` (per-feature drift + share + our
  decision). `_distil` applies **our** `config.DRIFT_SHARE_THRESHOLD` (0.5) to the per-column
  flags, not Evidently's default dataset-drift boolean, so the retrain policy is in one
  auditable place (`DriftReport.threshold` records it). `detect_drift(season=…)` = baseline vs.
  `season`-shifted; frames injectable for offline tests. Evidently imported lazily (`[ops]`).
- **`flows.py`** — `run_drift_retrain` = a Prefect flow `detect_drift → [if drift] → retrain →
  promote-or-hold`, retried tasks, in-process. The promote task is **`registry.promote`
  unchanged** (the F3 gate) → a `FlowResult` (`drift`, `retrained`, `promotion`, `promoted`).
  Prefect imported lazily inside the call so the package/core-CI never needs `[ops]`.
- **`config.DRIFT_SHARE_THRESHOLD = 0.5`** added next to `DRIFT_SEASON`.
- **`cli.py`** — `pdm monitor --season` (drift report + decision, exit 0/1) and `pdm flow
  --season/--seed/--min-delta` (the loop, exit 0 iff a model was promoted) go live; the two
  `_not_yet("F5")` stubs are gone. `test_skeleton` repurposed: **every** subcommand is wired
  (no stub left) — `--help` parses + exits 0 for each, none prints the honest-stub sentinel.
- **`pyproject.toml`** — `[ops]` capped `evidently>=0.4,<0.7` (ADR-013 reproducibility pin).
- **Tests** — `test_monitor.py` (6: shifted→drift, identical→stable, report covers exactly
  the feature columns, decision uses the configured threshold, human-readable summary),
  `test_flows.py` (5: drift triggers retrain + promotes, no-drift holds, **worse candidate
  held via `min_delta=-1.0`** — the gate guards the automated path, summary reflects the
  outcome). Offline, tmp SQLite, Prefect in-process, synthetic shift on the fixture. Both
  `importorskip` the `[ops]` libs. **121 total** (110 + 11); the 11 run where `[ops]` is
  installed (notebook/CI). **ADR-013.**

### F6 — Hosted free-tier deploy (2026-07-02)

- **`scripts/seed_demo_registry.py`** — `seed_registry(store_dir, seed=0)` trains on the fixture
  → registers the winner → promotes it to `production` through the **unchanged F3 gate**, into a
  self-contained SQLite store (explicit experiment `artifact_location` inside `store_dir` so DB +
  artifacts are colocated and the image can carry them). Pins a **class-rich fixture seed (0)** —
  seed 42 (the default) lands a single-class fixture split → `DegenerateSplit`. Tags the version
  `demo=fixture` + `provenance=…`. Returns the promoted version (str-normalised at the boundary,
  matching `registry`). A `--store-dir` CLI (`python scripts/seed_demo_registry.py --store-dir /mlflow`).
- **`Dockerfile.hf`** — a **self-contained** serving image (vs. the F4 mounted-volume `Dockerfile`):
  installs `[serve]`, bakes the demo registry at build (`RUN … seed_demo_registry.py --store-dir
  /mlflow`), runs as non-root UID 1000 (HF Spaces contract), `EXPOSE 8000`, `pdm serve`. Build path
  == run path (`/mlflow`) so the DB's absolute artifact URIs resolve at run time.
- **`docs/DEPLOY.md`** — HF Spaces front-matter (`sdk: docker`, `dockerfile_path: Dockerfile.hf`,
  `app_port: 8000`), push-to-Space steps, local build smoke-test, Render/Fly.io alternatives.
- **`.github/workflows/retrain.yml`** — the F5 placeholder replaced by a real `pdm flow --season`
  run; installs `[ops,generate]` (the loop regenerates the season-shifted window); real F3 gate
  (no `--min-delta` escape) so the scheduled loop can't auto-degrade.
- **Tests** — `test_seed_demo_registry.py` (4, offline, `[serve]`-gated): promotes a demo-tagged
  version, a fresh serving process over the baked store reads `model_loaded=true` + predicts (the
  F6 DoD in miniature), the store is self-contained (artifacts colocated), the bake is
  deterministic (same seed → same probabilities). **125 total** (121 + 4); the 4 run where
  `[serve]` is installed. **ADR-014.**
- **Run here:** the 4 F6 tests pass in isolation (`pytest tests/test_seed_demo_registry.py` → 4
  passed) + the native bake→serve round-trip. **Not run to completion here:** the *full* offline
  suite (retrains every fixture model across F2–F2.7 → minutes on the i3; stopped deliberately —
  same i3-slow-work deferral the repo already uses for F4/F5/F2.8) and the `docker build` (Docker
  Desktop daemon was down). Both run on CI on push; the full suite also on the notebook. Manual
  remainder: push to a Hugging Face Space + paste the live URL into the README (Jorge's HF account;
  DEPLOY.md has the steps).

## Next step (concrete)

**F7 (managed-cloud deploy — Cloud Run + Cloud SQL, ADR-015) is the current frontier — CODE +
SCAFFOLDING SHIPPED on the desktop (2026-07-02), the live URL lands on the notebook.** The
next concrete action, on the machine with `gcloud` auth + the Docker/Cloud Build path:

1. **One-time:** `gcloud auth login` → `gcloud config set project <id>` → enable
   `run` / `sqladmin` / `artifactregistry` / `secretmanager` / `cloudbuild` (DEPLOY.md lists it).
2. **Deploy:** `PROJECT_ID=<id> REGION=us-central1 bash scripts/deploy_cloudrun.sh` — builds
   `Dockerfile.hf` via Cloud Build, creates the Cloud SQL Postgres instance + the
   `DATABASE_URL` secret, rolls a Cloud Run revision wired to the SQL socket.
3. **Verify:** `curl $URL/health` → `model_loaded:true` (first hit cold-starts + bakes the demo,
   ~1–2 min); open `$URL/demo`, submit a prediction, confirm it appears in the recent panel
   (⇒ Cloud SQL logging works end-to-end).
4. **Record the green:** DONE on the notebook 2026-07-02 for F4/F7/F2.8 — full suite **133/137**
   (all F4 serve + 13 F7 `[cloud]` + F2.8 ceiling pass). The **4 F5 drift reds** (share-threshold
   arithmetic bug, see the top block) must be fixed before the suite is fully green.
5. **Then, and only then:** paste the `$URL/demo` link into the README (F7 DoD), and flip the
   **career-system managed-cloud gate** in `PERFIL_TECNICO.md` — *until the URL is live it is in
   progress, not closed* (Docker builds + a card-on-file GCP billing account are involved).

**F5 (drift monitoring + the auto-retrain loop, ADR-013) is DONE — the marquee shipped
(2026-07-02, desktop).** The complete production spine now runs end to end: **train → registry
→ serve → drift → retrain → cloud-scheduled**. The loop cannot auto-degrade because its promote
step is F3's `promote` unchanged (a worse candidate is held, proven by the `min_delta=-1.0`
test). `pdm monitor` / `pdm flow` are live — the last two roadmap stubs are gone.

**F6 (hosted free-tier `/health` link) is DONE & LIVE (2026-07-02, ADR-014):
https://jorgeed-forge-pdm-mlops.hf.space/health returns `model_loaded:true`.** Deployed to a
Hugging Face Docker Space off the `space-deploy` branch; three container-only bugs found and fixed
on the way (dockerfile_path ignored / installed-package data path / startup-bake — see the F6
verification note above). **The whole ROADMAP F0–F6 is shipped, and the spine is live.** Only the
notebook-side green record remains outstanding (independent of F6).

**Merge-at-home note (2026-07-02):** F5 was built on the desktop where `[ops]` isn't installed,
so `test_monitor`/`test_flows` (11 tests) **skip here** and must be run on the notebook/CI to
record their green. Two *separate* notebook-side items are still pending from before and are
**independent of F5**: the F4 clean-110 `pytest` run and the F2.8 GPU `pdm ceiling` numbers.
When pulling the desktop branch at home, fold those results in — they touch different STATE
lines (F4/F2.8) than F5, so the merge is additive.

**F2.8 — DONE (measured, 2026-07-02).** The GPU full-data `pdm ceiling` run + the TCN-OOF fold
are recorded above: the thesis is **refuted** (`ceiling_is_data = False`, stack beats best base
by +0.0073/+0.0063). Nothing outstanding on F2.8.

**F2.9 (RUL / graded label, ADR-011) and F2.10 (C-MAPSS, ADR-012) — FUTURE WORK, DEFERRED BY
DESIGN (2026-06-27, career-wide decision).** Both are scoped in full in `ROADMAP.md` but
intentionally **not built**: the rigor/honesty attitude is already proven (F2.5/2.6/2.7), an
over-deep F2 branch next to an unfinished gate *inverts* the signal, and RUL/C-MAPSS are a
deep-learning/benchmarking axis better owned by a dedicated DL showcase (or by making the private
[[project_fleet_ml]] browsable). Leaving them as **curated future work is itself the senior
signal** — knowing the next step and choosing the spine. Build only if a dedicated DL showcase is
decided.

**Honesty note (carries forward) — now measured, not asserted.** The real lift came from
the **data**, not the modelling: ADR-020's pre-failure degradation ramp + the
`vibration_mms` feature took the score ≈0.55→0.82. **F2.6 HPO, measured on the refreshed
0.2.0 data (notebook, seed 42, same cleaned frame), does *not* move it:** tuned − baseline
held-out test ROC-AUC = **+0.0034 (lightgbm 0.8118→0.8152) / 0.0000 (logreg
0.7131→0.7131)**; grouped-CV search 0.7526 / 0.6767; both pass `--audit` (lightgbm overfit
gap train−CV = 0.880−0.753 = 0.127 < 0.15; logreg train≈CV≈test). The value across F2–F2.6
is the *visible, ground-truth-scored process + the guards*, not accuracy — and the near-zero
HPO delta is itself the honest, postable confirmation of that on realistically learnable
data.

**F6 (hosted free-tier `/health` link, ADR-014) shipped at this boundary** — a self-contained
`Dockerfile.hf` bakes a fixture-trained **demo** registry so a fresh cloud deploy serves a real
prediction; the bake→serve cycle is verified natively and 4 offline tests pass. With F6 done,
**the full ROADMAP F0–F6 is shipped** (F2.9/F2.10 deferred by design); the production spine is now
complete *and reachable*. Offline core green on the i3 (the `[ops]` F5 tests + the `[serve]` F6/F4
tests skip where the extras aren't installed, run on the notebook/CI). **Manual remainder
(Jorge):** push to a Hugging Face Space + paste the live URL into the README (`docs/DEPLOY.md` has
every step). Still pending on the notebook (independent of F6): the F4 clean-110 run, the 11
`[ops]` F5 tests' green, and the F2.8 GPU `pdm ceiling` numbers — a merge that's additive across
different STATE lines.

## Notes

- **Cross-repo (2026-07-02): this repo owns the showcase's IaC / managed-cloud gate.** A
  parallel planning session on `receivables-agent` (its `PLAN.md` Phase 7) established the
  division of labor: forge-pdm already ships the heavier IaC rungs — `docker-compose.yml`
  (serve + MLflow UI, shared volume), `Dockerfile` + self-contained `Dockerfile.hf`, a **live**
  HF Space, and F7 **Cloud Run + Cloud SQL + Secret Manager** *as code* (`scripts/deploy_cloudrun.sh`,
  code shipped; live URL pending the notebook). So receivables deliberately keeps its IaC scope
  small (just upgrading its single-service compose to app+Ollama) rather than duplicating managed
  cloud here. **No action needed in this repo from that session** — this note is the pointer so a
  future session sees why the IaC weight lives here. The one open IaC-flavored item here is still
  F7's live URL + green record on the notebook (see "Next step").
- No GPU, no paid services, no training tokens — local NumPy/pandas + cheap models;
  MLflow on a local file backend; CI is free and offline.
- Determinism is a hard invariant: one seed → data → split → train, same metrics.
- The fixture vs. the full dataset distinction is load-bearing (ADR-001): clone-and-
  run convenience without ever reporting metrics off the reduced slice.
