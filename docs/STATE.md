# State — forge-pdm-mlops

Updated: 2026-07-14 (session: **F14a SHIPPED** — the web/worker split is real; F17 is next.)

## Current focus

**► ACTIVE PLAN — Epoch 2 (F10–F17): [`docs/EPOCH2_PLAN.md`](EPOCH2_PLAN.md).** Read that doc's
**§1 Locked decisions** before touching any Epoch-2 phase — it captures the irrecoverable reasoning
(why the sequence contender re-opens despite F2.7; committee-of-specialists; independent multi-mode
labels + the leakage subtlety; per-vehicle/region features = the report; optional enrichment;
GPU-train/CPU-serve; the $0 envelope; honesty boundaries; and **S1/S2**, the sequencing).
Execution order: **~~F14a~~ → F17 → F16 → F10 → F11 → F12 → F13 → F14b → F15**, one phase/session.
Compute is **notebook-only** (career memory `resources_compute`).

**Session 2026-07-14 — F14a (generate-your-own-data: the topology) is DONE (ADR-026).** The system
is now genuinely **two deployable units**, which is the thing F17 and F16 were blocked on — not the
report. A user sets a fleet size + window on `/demo`, the API answers **202 in ~16 ms**, a **Cloud
Run Job** (a different image, `Dockerfile.worker`) generates the fleet and writes it to Neon, the
page polls, then browses the data and shows **one risk score per vehicle**.

- **S2 shipped as written: generation is a separate deployable unit, not a `BackgroundTask`.** The
  API never runs the forge — `jobs.py` is the whole boundary (it starts a Cloud Run Job execution
  over the Admin API with stdlib `urllib` + the metadata-server token; no new dependency).
  **`test_the_api_never_generates_in_process` is the test that fails if someone reaches for the
  three-line `BackgroundTasks` version.** The two images have two dependency surfaces, which is the
  split made visible: `[serve,cloud]` (FastAPI + baked demo registry, warm) vs `[cloud,generate]`
  (the forge + the store, runs once, dies — no web server, no model).
- **The worker deliberately does NOT score.** It stores raw readings; the API scores them at **read
  time with whatever is promoted**, caching the roll-up per `(run, model version)`. Baking a score in
  at generation time was the easy path and would have silently broken the property the registry
  exists for (promote/rollback changes what is served, no redeploy — ADR-008/009). The cache key
  *is* the invalidation. **Do not "optimize" this by scoring in the worker.**
- **Both §5 open questions are RESOLVED — by measurement, and BOTH pre-registered candidates lost.**
  Details in **ADR-026** (its owner — don't restate them elsewhere):
  - **Caps** are a **storage** budget, not a timeout budget (a full run generates in ~1 s and scores
    in ~0.2 s). 30 vehicles × 14 days, binding cap **200 unit-days**, 200k rows retained,
    oldest-first eviction. Measured **~432 B/row** — my first ~300 B/row estimate was wrong and was
    corrected against the measurement.
  - **Roll-up** is the **peak of a 1-hour rolling mean** (unit ROC-AUC **0.983** on the full-data
    model), beating `max` (0.951, ±0.060) and `high-risk-share` (0.864). **`max` loses
    structurally**: it is a one-row statistic and the forge *deliberately injects outliers*, so one
    spurious spike flags a healthy vehicle — reproduced live (a healthy unit peaking at 0.85 while
    its sustained risk is 0.51). Verified on the **full-data** model, not just the demo one, so the
    finding isn't a fixture artifact (that model reproduced its recorded 0.8125 exactly).
- **An honest product constraint, found by measuring:** failures are hazard-sampled per step, so a
  short window often contains **zero** events (3/30 vehicles at 7 days). Hence the default of
  **20 vehicles × 7 days**, and the report claims *"which vehicles built toward a failure **during
  this window**"* — the forge only samples events *inside* the window, so "will it fail next week"
  has **no ground truth** and is not a claim this demo may make.
- **Next concrete step: F17 (Terraform / IaC).** Its entry question is the **state backend** — local
  gitignored state vs. a GCS free-tier bucket; the state file is secrets-adjacent (the Neon URL can
  land in it in plaintext) and never goes in git. The resource list is no longer hypothetical:
  `deploy_cloudrun_neon.sh` now creates **Artifact Registry · the API service · the generation Job ·
  Secret Manager · two IAM bindings** (the API may `run.invoker` **that job alone**) — read the
  script, don't re-derive it. F17's honest justification is unchanged: F7 shipped **imperative**, so
  there is no source of truth to diff, recreate or destroy.
- **ADR numbering:** forge-pdm D1–D7 still pre-assign **ADR-020–025** (unwritten — they land with
  their phases); **ADR-026 is written** (F14a). can-telemetry-forge from **ADR-021**.

---

**Session 2026-07-06 (notebook) — multi-mode demo fixture + grounded presets — DONE (ADR-019).**
Jorge caught the `/demo` presets all reading ~0.00% on the LIVE Cloud Run endpoint. Root cause:
the committed smoke fixture had only **one** failure mode (`oil_starve`), so the baked demo model
never learned overheat/bearing and correctly scored those presets near zero. **NOT the model or
the deploy** — the full-data ≈0.82 model always trains on all three modes; only the offline slice
was single-mode. Fix (ADR-019): (1) `build_sample.py` now **stratifies the fixture by event mode**
(quota per mode + healthy units, full 90-day window kept) and fails loud if a mode is missing →
~29k rows / 34 units / ≈940 KB, all three modes present; (2) each preset is now **grounded in a
real near-event fixture row** and validated against a freshly-baked model — **healthy 0.04% ·
overheat 99.2% · oil_starve 99.1% · bearing 99.7%**; (3) the `oil_starve` preset keeps its
era-NULL fields blank (older sensor era), the form clears them, and slider ranges were widened to
fit every preset. Full offline suite green (incl. two fixture-shape assumptions updated). Rebuilt
the image + **redeployed to Cloud Run + pushed to GitHub + redeployed to the HF Space**; all three
targets verified serving the same demo model (healthy 0.04% / overheat 99.2% / oil_starve 99.1% /
bearing 99.7%). Honest boundary intact (`demo=fixture`).
>
> **Follow-up (same day) — preset step-alignment.** Jorge caught that some preset values were off
> their input `step` grid (e.g. oil_starve boost 171 in a step-5 field, bearing rpm 1614 in step-10)
> → `<input type=number>` flags them invalid. Snapped every preset value to its grid (scores
> unchanged: 0.04 / 99.2 / 99.1 / 99.7) and added `test_preset_values_align_to_signal_step` so it
> can't regress. Rebuilt + redeployed all three targets.

> **Per-mode AUC (recorded 2026-07-06, `pdm ceiling` on the full 90-day data, seed 42).** The
> headline **overall held-out ROC-AUC = 0.8125** is an average that hides real per-mode spread —
> the honest per-row rung separates the three modes unevenly:
>
> | failure mode | held-out ROC-AUC | positives |
> |---|---|---|
> | `bearing` | **0.873** | 11,720 |
> | `oil_starve` | 0.796 | 18,159 |
> | `overheat` | 0.772 | 10,085 |
>
> By time-to-failure it sharpens toward the event, as intended: **0.92** within 24 h, 0.72 at
> 72–168 h, 0.56 beyond the horizon. Takeaway: strongest on the high-vibration bearing signature,
> weakest on overheat — worth stating rather than quoting a flat 0.82. (`ceiling.py`
> `by_mode`/`by_horizon` already compute this; now surfaced as a number.)

Remaining follow-up: a **plan for adding more failure modes** to `can-telemetry-forge` (below).

**Session 2026-07-06 — F9 (demo product-polish) is DONE (ADR-018).** The `/demo` page was too
technical (nine raw J1939 fields, bare-float result, one theme, English only); F9 makes it
**friendly, themed and bilingual — all self-contained (no CDN)**. Three pieces in `serve.py`:
**(1) Friendly inputs** — `_SIGNAL_META` gives each signal a unit + bounded `min/max/step` +
plain-language tooltip; **one-click `_PRESETS`** (*healthy / failing bearing / overheating*) fill
a whole plausible row (healthy = the form seed) so a domain-naive tester tries it in one click;
the result stays a **risk-meter with a plain-language band** (low/moderate/high — number + word
primary, colour a redundant cue). **(2) Light/dark theme** — CSS custom props, light default,
dark via `prefers-color-scheme` **and** a persisted `data-theme` override that wins both ways.
**(3) EN/PT-BR i18n** — `_DEMO_I18N` injected into the page as JSON (`ensure_ascii=False`, page is
UTF-8) + a tiny inline `t()` translator over the chrome, the signal **labels + tooltips**, and the
**preset names**; initial locale = persisted → else browser language (`pt*`→PT-BR). **Why JSON
injection:** the page is a `str.format` template (CSS/JS braces already doubled) — one JSON blob
each keeps the structured data out of the brace-doubling minefield. **Honesty boundary held
(ADR-018 / ADR-001):** i18n localizes the **UI shell only**; the `demo=fixture` banner + the ≈0.82
"reported result" framing are translated in *meaning*, never softened, and both survive in each
language (asserted). No new dependency, no endpoint-contract change (`/demo/predict`,
`/demo/upload` untouched). **Tests: 5 new in `tests/test_demo.py`** (theme-aware + self-contained;
both locales + honesty line held in each; friendly presets/units/tooltips; presets cover every
signal; ranges bracket the healthy seed) — verified green here; the existing demo tests stay green
(the full `test_demo.py` = 12 passed offline, incl. the training/persistence round-trips).
**Paired with `receivables-agent` Phase 9 (ADR-015)** for one shared design language (hypercube
navy+cyan). **The whole ROADMAP F0–F9 is now shipped.** ADR-018 written.

**Session 2026-07-05 — F8 (bring-your-own-data upload) is DONE (ADR-017).** The interactive
demo's missing third capability shipped: a tester can now **upload their own CAN/J1939 batch**
(CSV/Parquet) to `/demo` and get per-row failure probabilities + a summary — not just tune the
seeded sliders (F7). The hard part was **column names**: a real CSV never uses our exact nine
headers, so a new pure module **`src/pdm_mlops/upload.py`** does parse → **fuzzy `suggest_mapping`**
(stdlib `difflib` + a nine-entry J1939 synonym table, **no new dep** — auto-matched **9/9** on
realistically-renamed headers) → `build_frame` (unmapped signal → era-`NULL`, so a *partial*
dataset still scores, flagged "N of 9 provided") → fail-loud `assert_scorable` → `summarize`
(rows, % ≥ 50% risk, histogram). Wrapped by one two-mode **`POST /demo/upload`** (no `mapping`
= preview returns the suggested mapping; a confirmed `mapping` JSON = score). `_score` was
refactored to a shared **`_score_frame`** core so the JSON and upload paths score identically.
The `/demo` page gained a file-drop + a map-your-columns table + a scored-batch summary
(inline-JS histogram + first-50-rows table), still **no CDN**. **Guardrails fail loud** (2 MB
size cap read as `cap+1` so a huge upload can't wedge a scale-to-zero instance; 5k row cap;
non-J1939/prose file; no-mapping-selected; non-numeric mapped column) — all 4xx, never 500.
**No raw uploaded row is persisted** (an injected log stays empty after upload+score — a test
asserts it); the `demo=fixture` honesty banner is on the upload result too, and the response's
`demo` flag is **tag-driven** (a locally-trained untagged model honestly reports `demo:false`).
`python-multipart` added to `[serve]`. **Tests: `tests/test_upload.py` 25 green** (pure half
needs no extras; endpoint half `[serve]`-gated like `test_demo.py`) + the existing
`test_serve.py`/`test_demo.py` (16) unaffected by the `_score` refactor. **Next: F9** (demo
product-polish: friendly inputs + light/dark theme + i18n) — **to be done together with
`receivables-agent` Phase 9** (shared theme/i18n design language, same code where viable).

**Prior notebook session 2026-07-04 — F7 (managed-cloud deploy) is LIVE and the gate is CLOSED
(ADR-016).** The one gate F0–F6 left open — *operate a managed cloud runtime with a managed
resource in production* — is now closed on a real hyperscaler at **$0/mo**. Live:
**https://forge-pdm-mlops-958199756179.us-central1.run.app** — `/health` →
`{"status":"ok","model_loaded":true,"model_version":"1"}`, and `POST /demo/predict` returns
`persisted:true` (each served prediction is written to the managed Postgres and read back on
`/demo`). **Cost pivot (Jorge):** Cloud SQL has **no free tier** (~$8-10/mo), so the managed
*resource* is **Neon** (serverless Postgres, free tier) instead — same gate (managed runtime +
managed resource), **zero code change** (`store_pg.open_log` takes any SQLAlchemy URL; only the
`DATABASE_URL` secret differs). New `scripts/deploy_cloudrun_neon.sh` (Cloud Build → Secret
Manager → Cloud Run, no Cloud SQL); the original `deploy_cloudrun.sh` stays as the paid
alternative. **Two deploy bugs found + fixed live (ADR-016), both reusable:** (1) `Dockerfile.hf`
installed `.[serve]` not `.[serve,cloud]` → **no `psycopg` driver** → `open_log` swallowed the
connect error → `persisted:false` (graceful-degrade masking a missing dep; the endpoint never
broke, which is *why* it hid); (2) the `/demo` page + `store_pg`/`serve` docstrings hard-coded
"Cloud SQL" → corrected to "Neon Postgres" (the honest-output golden rule). Docs updated:
`README` (F7 badge + `/demo` link + F0–F7 status), `docs/DEPLOY.md` (Neon-free primary path).

> **Prior notebook session 2026-07-02 (verification/recording).** Full suite **133/137 green**
> (F4 serve, F7 store_pg/demo, F2.8 ceiling, F2.7 TCN); the 4 F5-drift reds **FIXED** (ADR-013
> follow-up); **F2.8 measured on full data** — the ceiling thesis **refuted** (see the F2.8 block).

> **F5 drift bug — FIXED (2026-07-02, ADR-013 follow-up).** Found on the first-ever real
> `[ops]` run (these tests had always skipped). Two never-run-code defects, both fixed:
> **(1) The share threshold was an artifact of the feature count.** `detect_drift` declares
> drift when the *share* of drifted features ≥ `DRIFT_SHARE_THRESHOLD`, which was `0.5` — set
> when `FEATURE_COLUMNS` had **8** signals and the synthetic heatwave stand-in shifts **4**
> (4/8 = exactly 0.5). The 0.2.0 refresh added `vibration_mms` (9 signals), so 4/9 = **0.44 <
> 0.5** → `drifted=False` while the tests assert `True`. **Decision (Jorge):** threshold →
> **⅓**, chosen against the physics — the real `heatwave` moves a *correlated cluster* of ~3-4
> of the 9 signals (`coolant_temp_c` via ambient; `oil_pressure_kpa`/`vibration_mms` via
> `wear_mult`), so ⅓ fires on a cluster and still rejects one or two noisy columns, independent
> of the feature count. The 4-signal test shift now clears it with margin (0.44 > 0.33), not on
> the boundary. **(2) The real `--season` path crashed.** `data.regenerate_full` stored the raw
> season *string* on the generator config, which expects a `Season` object → `AttributeError`
> in `config.validate()`; now calls `resolve_season(...)` like the generator's own CLI. Both are
> the same "tests that never run hide a defect" class. **All 4 reds green** (`pytest
> tests/test_monitor.py tests/test_flows.py` → 9 passed). Env seam noted in ADR-013: Evidently
> 0.6.7 + this NumPy trips `np.histogram` on the *full* regen (fixture/offline unaffected), so
> the end-to-end real-season breadth run waits on an Evidently/NumPy bump.

**F7 (Managed-cloud deploy — Cloud Run + managed Postgres — the managed-cloud gate) — LIVE
(2026-07-04, ADR-016; code/scaffolding shipped 2026-07-02, ADR-015).** This closes the one
gate F0–F6 deliberately left open: *operate a managed cloud runtime with a managed resource in
production* — the senior claim that "containerize an app" (F4/F6) is not. HF Spaces (F6) is free
hosting; F7 runs the **same** `Dockerfile.hf` image on **Google Cloud Run** (a managed,
serverless container runtime, not a VM) with a **managed Postgres** as the **managed resource**.
**Live at https://forge-pdm-mlops-958199756179.us-central1.run.app** (revision 00003), managed
resource = **Neon** (free-tier serverless Postgres; Cloud SQL is the paid alternative, see the
cost pivot in Current focus).

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
  > (Evidently + Prefect) is **not installed**, so `test_monitor`/`test_flows` **skip** there
  > and the rest of the offline suite stays green. **On the notebook (`[ops]` installed) they
  > ran for real and surfaced two never-run-code defects — both now fixed (ADR-013 follow-up):
  > the ⅓ share threshold and the `resolve_season` regeneration bug (see the F5 bug callout in
  > Current focus). `pytest tests/test_monitor.py tests/test_flows.py` → 9 passed.**

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

**F7 (managed-cloud deploy) is DONE & LIVE (2026-07-04, ADR-016) — the whole ROADMAP F0–F7 is
now shipped.** Cloud Run + Neon, `persisted:true`, live at
https://forge-pdm-mlops-958199756179.us-central1.run.app/demo. The deploy was done on the
notebook (`gcloud` CLI installed there, project `forge-pdm-mlops` with billing linked, 4 APIs
enabled).

**Career-system propagation — DONE this session (2026-07-04):** the managed-cloud gate was
**flipped to closed** in `PERFIL_TECNICO.md` (honest nuance kept: Cloud Run serverless-managed ≠
operating a K8s cluster, which remains the one open cloud sub-gate); the **achado** was recorded
in my private engineering-findings log (F7-LIVE entry, with the psycopg/graceful-degrade lesson); and
`APROFUNDAMENTOS.md` (#37), `POSTS.md` (F7 "$0 managed-cloud gate" + F7-b "healthy endpoint, DB
wrote nothing"), and `README_GITHUB.md` (F7 card + F0–F7 status) were updated.

**Next BUILD phases scoped 2026-07-04 (from Jorge's demo review) — pick one per session:**
- **F8 (bring-your-own-data demo)** — upload a CAN/J1939 batch to `/demo` → per-row predictions +
  summary, **including a map-your-columns step** (fuzzy auto-match + manual override) so *arbitrary
  header names* work and partial datasets score with missing signals as era-`NULL` — that's the
  "different column names we don't know of" versatility. Scoring core (`POST /predict`) already
  exists; F8 = upload + column-mapping + validate + summarize. ADR-017.
- **F9 (demo product-polish)** — the `/demo` is *too technical*: add **preset example buttons**
  (healthy/failing) + units/tooltips + bounded inputs + a **risk-meter** result; **light/dark theme**
  (keep the no-CDN constraint); **EN/PT-BR i18n** of the UI shell. Front-end showcase, **paired with
  `receivables-agent` Phase 9** for a shared design language. ADR-018.
- Also queued (career system, trivial): highlight both live demos more prominently on the GitHub
  profile (`README_GITHUB.md` — a "▶ Live demos" callout). See the sibling receivables PLAN Phases
  8–9 for the paired agent-reliability + demo-polish work.

**Cost note for future me:** the live service is $0 (Cloud Run scale-to-zero + Neon free tier),
but the GCP project has a **card-on-file billing account**. Tear-down if ever needed:
`gcloud run services delete forge-pdm-mlops --region us-central1` (+ delete the Neon project in
its own console). Nothing accrues while idle.

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
