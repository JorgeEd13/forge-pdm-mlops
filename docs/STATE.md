# State — forge-pdm-mlops

Updated: 2026-07-14 (session: **F17 SHIPPED** — the infra is Terraform; the IaC gate is CLOSED. F16 is next.)

## Current focus

> 🧊 **FROZEN for feature work since 2026-09-04 — study block open.** This repo was cut into
> **21 territories** by the APROFUNDAMENTOS programme (`repo-base-career/sistema/APROFUNDAMENTOS_ROADMAP.md`
> §R2). While the block is open, that programme reads this code line by line and measures its
> guards by mutation, so a moving tree would invalidate the measurements. **Epoch-2 phases F16 and
> F10–F15 are on hold**, and findings from the study go to this file's backlog rather than being
> fixed there. The freeze lifts when the R2 block closes. It is a *convention*, not a mechanism —
> nothing enforces it; Jorge can lift it by saying so.


**► ACTIVE PLAN — Epoch 2 (F10–F17): [`docs/EPOCH2_PLAN.md`](EPOCH2_PLAN.md).** Read that doc's
**§1 Locked decisions** before touching any Epoch-2 phase — it captures the irrecoverable reasoning
(why the sequence contender re-opens despite F2.7; committee-of-specialists; independent multi-mode
labels + the leakage subtlety; per-vehicle/region features = the report; optional enrichment;
GPU-train/CPU-serve; the $0 envelope; honesty boundaries; and **S1/S2**, the sequencing).
Execution order: **~~F14a~~ → ~~F17~~ → F16 → F10 → F11 → F12 → F13 → F14b → F15**, one phase/session.
Compute is **notebook-only** (career memory `resources_compute`).

**Session 2026-07-14 — F17 (IaC / Terraform) is DONE (ADR-027). The IaC gate is CLOSED.** The managed
deploy is now **defined in `terraform/`**, not performed by a script. `terraform plan` against the
live deploy is **clean** ("No changes. Your infrastructure matches the configuration"), which is the
DoD: proof the config describes reality rather than merely looking plausible.

- **Adopted the live infra with config-driven `import` blocks, not a recreate.** 12 resources
  imported, 0 destroyed. Recreating would have deleted the Artifact Registry (taking both images with
  it) and dropped the live demo to prove a point. The drift found while converging is **explained,
  not hidden**: `client="gcloud" → null` on both Cloud Run units (literally the imperative→declarative
  handover stamped into the resource) and an Artifact Registry description that still said "serving
  images", untrue since F14a added the worker image.
- **State: a private, versioned, REGIONAL GCS bucket** (`forge-pdm-mlops-tfstate`, 32 KB). Checked
  against zero-budget *before* committing: Always Free covers 5 GB of **regional** us-central1
  standard storage — **multi-region `US` is NOT free-tier eligible**. The state file is **not source**
  (it is Terraform's *belief* about what exists), never goes in git, and is **secrets-adjacent**: the
  Neon URL lands in it **in plaintext**, because `sensitive = true` redacts CLI output and does *not*
  encrypt state. That is why the backend is a bucket with public-access-prevention enforced.
- **Neon is NOT managed by Terraform** — it is not a GCP resource. It stays a documented manual
  prerequisite whose URL is fed in as a `sensitive` var via `TF_VAR_`. The community provider wants a
  Neon API key (a *new* secret class) and the password would land in state anyway, so it buys no
  hygiene. Honest cost: `apply` in a fresh project does **not** give a working system until Neon exists.
- **⚠ THE FINDING — codifying the IAM proved the permission model was WRONG, and an over-privileged
  account had been hiding it.** The script ran both units as the **default compute SA**, which holds
  **`roles/editor`** — so its two "least privilege" bindings were **decorative**. F17 gave each unit a
  dedicated SA (`forge-pdm-api`, `forge-pdm-worker`)... and generation **immediately 403'd**:
  `Permission 'run.jobs.runWithOverrides' denied`. **`roles/run.invoker` — the role the script granted,
  the role that "worked" for months — does not contain `runWithOverrides`**, and `jobs.py` starts the
  job *with per-request env overrides* (that IS the mechanism). It only ever worked because Editor
  contains the permission. The binding wasn't just decorative — **it was insufficient.** Correct role:
  `roles/run.jobsExecutorWithOverrides` (3 permissions), not `run.developer` (88). **Lesson: an IAM
  binding never tested against a least-privilege identity is a binding you have written, not verified.**
- **⚠ TERRAFORM DOES NOT KNOW WHETHER YOUR APP WORKS — it printed `Apply complete!` over a broken
  system TWICE this phase.** (1) Over the 403 above: the resources matched the config exactly; the
  config was wrong. (2) Over a job stuck in `Ready=False / SecretsAccessCheckFailed` from an earlier
  failed apply — by then the spec *and* the IAM were correct, so **Terraform saw no diff and did
  nothing**; a failed condition is *health*, and health is not in the spec. Needed an explicit
  `-replace`. **Terraform reconciles configuration, not health.** Both were caught by an end-to-end
  request. **`plan` is not a test.** Never claim IaC validates the app.
- **Two Terraform lessons kept:** (a) **implicit dependencies come from *references*** — an IAM
  binding nothing references is invisible to the graph, so the first apply deployed the job under an
  identity that could not yet read its secret, and Cloud Run rejected the revision. `plan` cannot show
  this (it renders resources, not order) → explicit `depends_on`. (b) **`:latest` is mutable and
  silently breaks a declarative deploy** — same tag in, no diff, no new revision, code built and *not
  serving*. Images are now tagged with the **git SHA**.
- **`scripts/deploy_cloudrun_neon.sh` NO LONGER CREATES INFRASTRUCTURE.** It builds the two images
  (the one thing Terraform doesn't do) and calls `terraform apply`. Leaving it able to create
  resources would have shipped **two sources of truth** — the exact defect F17 removes. The old
  `deploy_cloudrun.sh` (paid Cloud SQL) is marked **SUPERSEDED — do not run** (it would stand up a
  parallel stack the state knows nothing about). Tear-down is a documented **two-step**
  (`deletion_protection` defaults **on**).
- **VERIFIED LIVE, end to end, under the new least-privilege identities (2026-07-14).** `/health` →
  `model_loaded:true`; a real generation (8 vehicles × 7 days) → **202 `worker: cloudrun-job`** → the
  Cloud Run Job ran → **16,128 rows written to Neon** (exactly `expected_rows`) → the per-vehicle
  report rendered, **1 of 8 flagged** at risk ≥ 0.7, roll-up rule intact ("peak of a 1-hour rolling
  mean… sustained risk, not a single spike"). Cold start held at **~2 min**, as documented.
- **A carried-over to-do, INVESTIGATED AND KILLED rather than done.** F14a wanted `--cpu-boost` on the
  generation Job. **It does not exist for Jobs** — `startup_cpu_boost` is a **service-only** knob, and
  rightly so: boost fixes a pathology only services have (CPU throttled outside request handling). A
  job is not request-driven and already has full CPU for its whole execution. **So the ~1–2 min is not
  a CPU problem and no infra flag will fix it.** The real cause: **`mlflow` + `lightgbm` +
  `scikit-learn` are BASE dependencies**, so the worker pulls and imports the whole training stack —
  *which it never uses*, because ADR-026 deliberately had it not score. This also weakens the "two
  dependency surfaces" claim: the split is real at the **extras** level, but both images sit on a base
  carrying the training stack. **Real fix = a lighter worker image** (move the training stack to its
  own extra) — touches every image, so it is its own piece of work.
- **Deploy targets, all three settled (Fluxo L item 11):** F17 touched **zero application code** (no
  `src/`, no `tests/`, no `pyproject.toml`, no Dockerfile) — so **nothing was owed to any target**.
  **Cloud Run** is already at the F17 state (`terraform apply` *is* the deploy). **GitHub** = pushed.
  **HF Space** = already live and in sync on app code (`git diff main space-deploy -- src/ tests/
  pyproject.toml` → **empty**).
  > ⚠ **Expected, benign divergence — do NOT "fix" it:** `git diff main space-deploy -- scripts/` shows
  > `deploy_cloudrun.sh` + `deploy_cloudrun_neon.sh`. Those are **GCP-only** scripts the Space never
  > runs, and F17 rewrote them on `main`. The Fluxo-L item-11 check includes `scripts/` in its diff, so
  > **it will flag this next session as a false positive.** It is not divergence that matters: the app
  > code is identical. Syncing GCP deploy scripts onto the Space branch would add noise, not safety.
- **⚠ OPEN — the `/demo` product shape is WRONG, and it was never written down (recorded 2026-07-14,
  from Jorge). NEW: `EPOCH2_PLAN.md` §1 **D8** (the shape) + **G2** (a real multi-tenancy defect).**
  Read D8 before touching the page layout. Three concrete defects, none of them yet fixed:
  1. **Generation is buried.** The page order is single-row form → upload (F8) → **Generate fleet**
     (3rd) → *Recent predictions*. The intended product is **generate-FIRST**: pick params →
     generate → **inspect the dataset in a scrollable grid** (a "VS Code Data Wrangler"-like view,
     so the synthetic data is *seen*, not asserted) → committee scores it (D2) → **full report**
     (D5/F12). F14a built the machine and left it in the basement. **This is a layout defect, not a
     missing feature** — and the `/demo` may **not** be called "done" until D8 is real.
  2. **The theme button renders EMPTY until the first click.** Diagnosed, not guessed:
     `applyTheme()` is the **last statement inside `applyLang()`** (`serve.py` ~L1464) and is called
     nowhere else at startup — while the *click* handler calls it directly. So **any** throw earlier
     in `applyLang()` (`buildFields()`, or the F14a-added `buildGenFields()`) silently leaves the
     button unlabelled, and clicking "fixes" it. **Fix = decouple theme init from `applyLang()`**;
     don't paper over it — find out *what throws*, because that throw is also skipping whatever else
     came after it.
  3. ***"Recent predictions — logged to a managed Postgres instance (Neon)"* sits directly under the
     generate panel**, where it reads as if it belonged to fleet generation. It does not: it is the
     **F7 single-row prediction log**. Wrong neighbour, wrong implied ownership.
- **⚠ G2 — a REAL multi-tenancy defect, not cosmetic (see the plan).** `store_gen.prune()` evicts the
  oldest **finished** runs against a global 200k-row cap. It protects in-flight runs and the run being
  handed back — but **not a finished run a user is still reading**. Under concurrent visitors (the
  *success* case for a public demo) **one user's fleet is deleted mid-browse by another user's
  generation**. Jorge's direction: keep the generated dataset **local to the browser** so simultaneous
  users can't evict each other or exhaust the free tier. **⚠ Do NOT "fix" it by raising the cap**
  (delays the collision, doesn't remove the race, risks the free tier — G1), and **do NOT let a
  local-storage design quietly collapse the web/worker split (S2)**, which is what makes F16/F17 honest.
- **Fluxo K ran (2026-07-14) — and it DELIBERATELY HELD one thing. Do not "helpfully" add it back.**
  The GitHub profile card gained **one** bullet: the **Terraform / IaC + separate worker job** axis
  (done, honest, no traffic risk). It did **NOT** gain the *"generate your own fleet"* capability,
  even though that shipped in F14a and works. **Why the hold:** the profile README is a **traffic
  driver**, and **G2 gets WORSE with traffic** — advertising a feature whose known defect is "a
  concurrent visitor's fleet is deleted mid-browse" points an audience straight at the failure. It is
  also buried third on the page (D8). **Release the hold once D8 + G2 land**, and it becomes a strong
  claim instead of a liability. (Jorge's standing constraint: don't overflood the public README with
  material that will need sanitizing later.)
- **Next: F16 (Kubernetes on `kind`).** ⚠ Its justification is **the MARKET, not the project** — do
  **not** defend it the way F17 was defended. F17 fixed a defect this repo genuinely had; F16 will
  not, and must say so out loud (`EPOCH2_PLAN.md` §3, F16's ⚠ block).

---

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
- **VERIFIED LIVE on Cloud Run + Neon (2026-07-14).** A real generation — 12 vehicles × 7 days —
  kicked off from the deployed API, ran on the **Cloud Run Job**, stored **24,192 readings** in Neon,
  and rendered the per-vehicle report:

  ```
  model=v1  demo=True  scored=24,192 rows
  flagged 3 of 12 vehicles @ risk >= 0.7
    FLAG u0001  risk=0.996  peak=0.998  share=21%
    FLAG u0009  risk=0.915  peak=0.989  share=6%
    FLAG u0010  risk=0.728  peak=0.962  share=27%
         u0003  risk=0.680  peak=0.983  share=1%   <-- a `max` roll-up FLAGS this one
         u0006  risk=0.644  peak=0.947  share=26%  <-- and this one
  ```

  **That output is the ADR-026 roll-up finding reproducing in production.** `u0003` peaks at **0.983**
  on a single reading and is correctly *not* flagged (sustained risk 0.680); ranking on `max` — one of
  the two rules §5 pre-registered — would flag it *and* `u0006`, turning a 3-of-12 report into
  5-of-12. The kick-off POST answers **202 with `worker: cloudrun-job`**, and the `demo=fixture`
  banner holds on the roll-up. **Honest caveat:** the worker is a cold-starting container carrying
  LightGBM + MLflow + the forge, so `queued → running` takes **~1–2 min** on a cold Job. It is async
  and nobody is blocked, but the demo is **not snappy** — do not describe it as if it were. (Job
  cold-start is a candidate for the same `--cpu-boost` treatment the API got.)
- **HF Space: PROPAGATED and live on F14a — and the "blocker" was never the Space.** The first
  attempt died on a `git lfs` smudge (`assets/logo.png` → *"Object does not exist on the server:
  [404]"*), which I initially read as a broken Space. **It was not.** `main` is deliberately
  **LFS-free**, so those objects were never pushed to GitHub — they live **only** on the HF remote.
  With no `lfs.url` set, git-lfs resolved the download against the **default remote (`origin` =
  GitHub)**, which correctly answered "I don't have that object". Pinning `lfs.url` at the Space
  fixed it in one line (inert on `main`, which LFS-tracks nothing). **Live now:** `/health` →
  `model_loaded:true`, and `/demo` renders the generate panel **correctly disabled**
  (`GEN_ENABLED = false`) with `POST /demo/generate` → an honest **503** naming what is missing —
  the graceful-degrade contract working on a target that genuinely has no database and no worker.
- **⚠ `scripts/deploy_space.sh` had TWO real bugs, both now fixed — the second one I introduced.**
  (1) **Its no-arg mode was broken by construction:** it cherry-picked "every commit on `main` that
  `space-deploy` doesn't have", but the histories are **intentionally unrelated**, so
  `space-deploy..main` is *every* commit on main — with no args it replayed the repo from **F0** and
  drowned in add/add conflicts. It only ever looked like it worked because it was always called with
  explicit commit arguments. The deploy is a **content sync**, not a history replay, so it now syncs
  paths and makes one honest commit. (2) **LFS binaries cannot be synced with `git checkout main --
  <path>`** — that stages main's blob *verbatim*, **bypassing the LFS clean filter**, so the commit
  carries raw bytes and HF's pre-receive hook rejects the whole push. LFS paths are now written to
  the worktree and `git add`-ed so the filter runs, with a guard that fails loud (legibly) if a
  staged blob under an LFS path is not a pointer. The script also refuses to run on a dirty tree and
  **fails loud if `Dockerfile` / `README.md` / `.gitattributes` are ever staged from main** —
  overwriting the Space's `Dockerfile` is F6 bug #1, which served a no-bake image for weeks.
  **`README.md` is deliberately NOT synced:** main's now advertises "generate a synthetic fleet",
  which does **not** work on the Space — copying it would make the Space claim a capability it
  doesn't have.
- **⚠ Deploy bug, found on the first real build of the worker image (fixed) — a CONTAINER-ONLY defect,
  same class as the three F6 hit.** The build died on `No matching distribution found for
  can-telemetry-forge==0.2.0`: **the generator is not on PyPI** — it is the companion repo, installed
  *editable* from a sibling checkout on every dev machine, which is precisely why nothing local ever
  noticed. **Fix:** the worker image installs it from its **public git remote at an immutable commit**
  (`FORGE_REF` build arg) before resolving `.[cloud,generate]`; a SHA is *stronger* than the `==0.2.0`
  pin, not weaker. **Reusable lesson: an editable install of a sibling repo is a dependency you have
  not actually declared** — invisible until something builds clean. The serving image never hit it
  (it doesn't install `[generate]`); splitting into two images is what surfaced it.
- **⚠ A SECOND container-only bug, on the worker's first real cloud run — and it is one F6 ALREADY
  documented.** The Job started, then died on `FileNotFoundError:
  '/usr/local/lib/python3.12/configs/dataset.json'`: **an installed package resolves data files off
  site-packages, not the repo** (`config.REPO_ROOT` = `Path(config.__file__).parents[2]`). That is
  **bug #2 of ADR-014, verbatim**, one image later against a different file. **Fix:**
  `config.dataset_config_path()` (env override → source tree → `./configs/`, failing loud and naming
  site-packages), `DATASET_CONFIG` set in `Dockerfile.worker`, and a test that pins it. **The
  uncomfortable lesson: a documented bug is not a fixed CLASS of bug.** ADR-014 wrote this defect down
  in plain English and it still recurred the moment a new deployable unit appeared — because the ADR
  fixed the *instance* (the fixture path), not the *shape* (any repo-relative path in an installed
  package). What generalizes is a test, not a paragraph. **The crash-honest design did pay off
  though:** the worker died in another process, in the cloud, and the reason still landed on the run
  row where the poller could see it.
- ~~**Next concrete step: F17 (Terraform / IaC).**~~ **DONE — see the F17 block above (ADR-027), which
  owns this now.** For the record, F17's entry question (the state backend) resolved to a **private,
  versioned, regional GCS bucket**, and the script's `run.invoker` binding — described here as "the
  API may `run.invoker` **that job alone**" — turned out to be **the wrong role entirely**. It does
  not contain `run.jobs.runWithOverrides`.
- **ADR numbering:** forge-pdm D1–D7 still pre-assign **ADR-020–025** (unwritten — they land with
  their phases); **ADR-026** (F14a) and **ADR-027** (F17) are written. can-telemetry-forge from
  **ADR-021**.

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

- **Study backlog, queued 2026-09-04 (APROFUNDAMENTOS `R2-T1`, data + feature contract):** four
  findings over `src/pdm_mlops/data.py` and `src/pdm_mlops/features.py`, **none fixed** — the study
  programme documents, it does not repair. Measurement baseline for the scoped suite
  (`tests/test_data.py tests/test_features.py tests/test_suspect.py`): **17 passed**, 9.6 s; tree
  restored afterwards (`git checkout -- src/pdm_mlops/features.py`). Shape numbers come from a real
  full regeneration on this machine (`data.load_readings()`, 26 s): **3,473,280 rows · 19 columns ·
  134 units**, target rate 5.17%; `features.prepare()` splits it into **100 train / 34 test units**
  (2,592,000 / 881,280 rows; 5.38% / 4.53% positive). A control mutation (`GroupShuffleSplit` →
  `ShuffleSplit`) produced **5 failures**, so the green results below are missing coverage, not a
  dead suite. Nothing leaks today — the `FEATURE_COLUMNS` allowlist is what holds, and the shipped
  split carries exactly those nine columns. **T1-2 is 🔴 a wrong sentence in a committed public
  document** (ADR-003 states a column count that is off by one) and should be corrected on its own,
  ahead of the rest; T1-1 is an *unsupported* claim rather than a proven-false one; none of the four
  is an externally reachable hole (no external attack path was exercised — the claim here is only
  that these findings are not one).
  - **T1-1 — the `can-telemetry-forge==0.2.0` pin does not pin bytes, so "byte-identical across
    machines" is unsupported.** `pyproject.toml` L73 pins the `[generate]` extra, and both
    `data.py`'s module docstring and ADR-001 claim every machine regenerates byte-identical data
    from the same config + pinned generator. **Measured:** on this machine that requirement resolves
    to an *editable* install of a sibling working tree —
    `importlib.metadata.distribution('can-telemetry-forge').read_text('direct_url.json')` returns
    `{"dir_info": {"editable": true}, "url": "file:///home/.../public/can-telemetry-forge"}` — and
    `git -C ../can-telemetry-forge describe --tags` returns `fatal: No names found`, i.e. no tag
    anchors `0.2.0`; the version string is hand-written in the neighbour's `pyproject.toml`. Any
    commit in the generator changes the data without changing the pin, and CI never installs the
    generator, so nothing catches it. Fix: record the generator commit (or the parquet hash)
    alongside the generated dataset, and soften the ADR sentence to what is actually enforced.
  - **T1-2 — ADR-003 says "the eight J1939 channels"; `FEATURE_COLUMNS` has nine.**
    `vibration_mms` was added when the generator's ADR-020 made vibration a learnable channel; the
    module comment in `features.py` L35–37 records that, the ADR text does not. **Measured:**
    `len(features.FEATURE_COLUMNS) == 9`. Documentation-only, but it is the document a reviewer
    reads first. Fix: a dated addendum in ADR-003, not a silent rewrite.
  - **T1-3 — `assert_no_leakage` is an enumerated denylist of 4 names over a 19-column table, and
    the committed fixture hides the gap.** `anomaly_signal` is written by the same labelling pass as
    `anomaly_type` and is **not** in `LEAKY_COLUMNS`. **Measured:** `features.assert_no_leakage`
    **passes** on a frame whose columns are `engine_speed_rpm`, `anomaly_signal`, `runtime_hours`
    and `equipment_age_days` — i.e. it does not object to `anomaly_signal` (it still raises, as
    designed, on any frame that does carry one of the four listed names, so this is about the gap in
    the list, not a broken guard). On the full frame, `anomaly_signal` is non-empty on **exactly the
    same 159,164 rows** as `anomaly_type`, which is the execution-level evidence that the two come
    from the same labelling pass (the generator's own column table in
    `../can-telemetry-forge/src/can_telemetry_forge/io/writers.py` L141 describes it as "which
    signal carries the row's labeled defect"). And `data/sample_readings.parquet` has **15 columns**, without
    `anomaly_signal`, `t_index`, `runtime_hours` or `equipment_age_days` — so no test on the fixture
    can ever exercise the gap. Nothing leaks today: the `FEATURE_COLUMNS` allowlist is what actually
    holds. Fix: derive the check from the source schema (flag any column that is neither an
    allowlisted feature nor an explicitly declared covariate) instead of listing four names.
  - **T1-4 — the leakage guard is name-based, so the derived-feature branch is guarded but blind.**
    In `features.prepare`, `suspect_feature=True` passes the **whole** `readings` frame (target
    included) to `suspect.compute_suspect` and then re-runs `assert_no_leakage` on the augmented
    matrix. The guard only compares column names, so a suspect score that used the target would
    still be called `signal_suspect` and pass. **Measured:** deleting that `assert_no_leakage` call
    leaves the scoped suite at **17 passed** (`tests/test_suspect.py` included) — it has never once
    fired in a test. Fix: pass only the signal columns into `compute_suspect`, or add a provenance
    check; a name check cannot express this invariant.
  - **Suite-level finding (same session, no separate item):** disarming the leakage guard entirely
    (shrinking `LEAKY_COLUMNS` to the target *and* deleting both `assert_no_leakage` call sites) and
    running the **full** suite gives **2 failed, 203 passed** (697.8 s, 205 tests collected). Both
    failures are in `tests/test_ceiling.py` (`test_honest_frame_rejects_leak_features`,
    `test_leak_features_are_label_side_only`), which pass on the clean tree (13 passed, 61.8 s);
    `tests/test_features.py` stays **fully green** with the guard gone. What caught the mutation is
    `ceiling.py` keeping its own `LEAK_FEATURES` list and asserting `"failure_mode" in
    features.LEAKY_COLUMNS` from outside — a cross-module drift guard that covers **one** of the
    four names. `anomaly_type` and `is_outlier` could leave the denylist with no test going red.

- **Study backlog, queued 2026-09-04 (APROFUNDAMENTOS `R2-T2`, the two contenders + tracked
  training):** six findings over `src/pdm_mlops/models.py` and `src/pdm_mlops/train.py`, **none
  fixed** — the study programme documents, it does not repair. Measurement baseline for the scoped
  suite (`tests/test_models.py tests/test_train.py`): **14 passed**, 56.8 s; tree restored
  afterwards (`git checkout -- src/pdm_mlops/models.py src/pdm_mlops/train.py`). Numbers come from a
  real run on the committed fixture at `seed=0`: 21,600 x 9 train / 7,776 x 9 test rows, **25 train
  units / 9 test units**, target rate 5.28% / 3.82%, **21.41% NaN** in the training matrix; ROC-AUC
  **logreg 0.6896**, **lightgbm 0.7041** (winner, registered as v1). Six mutation points were run
  (ceiling per the study brake; the flavor one has two variants, so seven runs) and **four came back
  green**. Two control mutations went red
  (`max` -> `min` on the winner: 1 failed; inverting the flavor dispatch for both models: 6 failed
  in 9.5 s), so the scoped suite is not dead — the green results below are missing assertions.
  **T2-6 is the one that matters**: registering the *loser* instead of the winner is green across
  the **full 205-test suite** (698.9 s), so the repo cannot currently tell those two apart. Nothing
  here is externally reachable (no external attack path was exercised — the claim is only that these
  are not one) and no committed public document is factually wrong, so no item is 🔴 URGENT under the
  study brake's narrow valve; T2-6 is the first line to fix when the block unfreezes.
  - **T2-1 — `train.py`'s module docstring and `train()`'s docstring both say "file" MLflow
    backend; it is SQLite.** L10 says "a **local file MLflow backend**" and the `tracking_uri` arg
    says "Tests pass a tmp `file:` URI", while L133 calls `config.sqlite_tracking_uri(...)` and
    `tests/test_train.py` L32 builds a SQLite URI. Stale since ADR-004 moved off the file store
    (MLflow 3 put it into maintenance mode). Docs-only, no runtime effect. Fix: correct both
    sentences to SQLite.
  - **T2-2 — `build_all` silently drops an unknown *model* name in `tuned`.** The loop iterates
    `BUILDERS` and reads `tuned.get(name)`, so `tuned={"lgbm": {...}}` is ignored without error:
    the run trains at baseline and is logged `tuned=False`, while `clean` still flips to `True`
    (because `clean = tuned is not None`) — a half-applied configuration. **Measured:**
    `build_all(seed=0, tuned={"nosuchmodel": {"C": 3.0}})` returns both baseline models with **zero
    warnings**, while `tuned={"logreg": {"bogus_param": 1}}` raises
    `ValueError: build_logreg: unknown hyper-parameter(s) ['bogus_param']` — unknown *parameter*
    keys raise, unknown model names do not. No production caller in `src/` builds `tuned` outside
    `BUILDERS` (`tune.run`, `cli.py`), but `train.train(tuned=…)` and `build_all(tuned=…)` are
    public API and `tests/test_train.py` already passes a hand-written literal, so this is a latent
    trap with the door open, not an unreachable path. Fix: validate the keys of `tuned` against
    `BUILDERS` and raise, mirroring `_check_overrides`.
  - **T2-3 — the artifact-flavor dispatch is a string comparison and nothing asserts the recorded
    flavor.** `_flavor_log_model` branches on `if model.name == "lightgbm"`, so any other name
    (a renamed or third contender) falls through to the sklearn flavor. **Measured:** routing
    LightGBM through the sklearn flavor (`if False:`) leaves the scoped suite at **14 passed**, and
    it runs in **31 s instead of 57 s** — the only visible signal, and no test measures it. The
    artifact loses the native-flavor metadata that the serving layer's reload depends on. Fix: an
    assertion on `mlflow.models.get_model_info(...).flavors`, plus a dispatch table keyed on an
    explicit flavor field so an unknown name raises.
  - **T2-4 — `seed` is logged twice per run.** `mlflow.log_params(model.params)` already carries
    `"seed"` (every builder puts it there) and the next line logs `"seed"` again. Safe only because
    both sides always hold the same value; MLflow raises on a conflicting re-log, so a future
    builder that derived its seed would blow up in `train()` for a reason unrelated to the cause.
    Fix: drop the explicit line, or stop putting the seed in `params`.
  - **T2-5 — `register_model` is called with the legacy `runs:/<id>/model` URI and MLflow 3 resolves
    it by fallback.** **Measured** on a real run: "WARNING mlflow.tracking._model_registry.fluent:
    Run with id <id> has no artifacts at artifact path 'model', registering model based on
    models:/m-<id> instead". Registration works (v1 created); the risk is a deprecation with a
    countdown, the third time this stack has moved under this file (file store, `skops`, now this).
    Fix: register the `ModelInfo` returned by `log_model` directly.
  - **T2-6 — no test ties the registered version back to the winning run.** The F2 DoD test asserts
    `registered_version is not None`, that exactly one version exists, and that its number matches
    the summary — never *which run* it came from. **Measured:** changing the registration to
    `results[0].run_id` (the LogReg, measurably the worse model: 0.6896 vs 0.7041) leaves the scoped
    suite at **14 passed** and the **full suite at 205 passed** (698.9 s) — the blindness is
    repo-wide, not local to one test file — while `format_summary` still prints
    `lightgbm 0.7041  <- winner`. Report
    and registry can disagree in silence, which breaks the project's central promise ("which model
    is current and on what evidence") at exactly the link that makes it verifiable. F3's promotion
    gate and F4's alias-based serving build on top of this link. Fix: one assertion —
    `assert versions[0].run_id == summary.winner.run_id`. **Do this one first when the R2 block
    unfreezes.**

- **Study backlog, queued 2026-09-08 (APROFUNDAMENTOS `R2-T3`, the unsupervised detection
  ladder):** five findings over `src/pdm_mlops/detect.py` and `tests/test_detect.py`, **none
  fixed** — the study programme documents, it does not repair. Measurement baseline for the
  scoped suite (`tests/test_detect.py`): **10 passed**, 8.0–10.0 s, torch present so the
  autoencoder test runs. All numbers below come from the committed smoke fixture (29,376 rows,
  34 units, 9 signal channels, 4.05% labelled outlier rate) with each detector **fit and scored
  on that same frame**, which is what `fit_score_all` does; per-family recall is measured at a
  top-2% alarm budget (588 rows). Five mutation points were run (the study brake's ceiling) and
  **four came back green**; one control went red, so the suite is not dead. `detect.py` was
  restored to HEAD afterwards and the scoped suite re-run green.
  **T3-1 is the one that matters** — it is a live loss of detection quality, not a latent trap.
  Nothing here is reachable from outside the process and no committed public document is
  factually wrong (ADR-005 correctly describes what the code *intends*), so no item is 🔴 URGENT
  under the study brake's narrow valve.
  - **T3-1 — min-max scaling erases the Mahalanobis view; the multivariate rung has one view,
    not two.** `MultivariateDetector.score` min-maxes each view and combines with
    `np.maximum`, promising "suspect if *either* view flags it". **Measured:** raw Mahalanobis
    on this fixture has median **8.63**, p99 **6,291.9** and max **660,557.4** — the max is
    ~76,000x the median — so after min-max its median is **1.18e-5**. `maha > iso` on **0.19%**
    of rows; `combined == iso` on **99.81%**. Cost at the 2% budget: `joint_outlier` recall
    **0.104** as shipped vs **0.391** using raw Mahalanobis alone and **0.353** with a
    percentile-rank transform instead of min-max; `obvious_outlier` **0.377** as shipped vs
    **1.000** raw. Mutating `combined = np.maximum(iso, maha)` to `combined = iso` leaves the
    scoped suite at **10 passed** — it is nearly a no-op, which is the point. The rung exists
    for the joint outlier, the one family no per-column check can see. Fix: rank- or
    quantile-transform before combining (or keep fitted quantiles), plus one assertion —
    `assert (maha > iso).mean() > 0.05`.
  - **T3-2 — the suspicion score is batch-relative, and there is a fixed threshold downstream.**
    `_minmax01` takes `lo`/`hi` from the scored batch. **Measured:** scoring only the 28,186
    rows with no outlier label still yields a max of **1.0** and **14 rows above 0.9** — a
    perfectly clean batch manufactures a worst case; and the same five clean rows score
    `0.8949 / 0.7158 / 0.8404 / 0.3070 / 0.2522` in the clean-only batch vs
    `0.8494 / 0.6795 / 0.7977 / 0.2914 / 0.2394` in the full batch. ADR-005 §7 has
    `data_quality_check` comparing a batch's suspect rate against a fitted baseline and F5
    reuses it as a drift signal, i.e. a fixed threshold over a ruler that changes per batch.
    (How `suspect.py` consumes these scores was not exercised here — the measurement is a
    property of `detect.py`.) Fix: store fit-time quantiles and score against them, so the
    scale is absolute and comparable across batches.
  - **T3-3 — `TemporalDetector` learns channel eligibility on the very batch it scores, so it
    goes blind on the most broken batch.** `fit` marks a channel freeze-detectable when its
    baseline exact-repeat rate is under `CONTINUOUS_REPEAT_MAX` (1%), and `fit_score_all` fits
    and scores the same frame. The unsupervised selection works and is worth keeping — on this
    fixture it keeps 7 of 9 channels and drops exactly `oil_pressure_kpa` and
    `boost_pressure_kpa` (the ones that legitimately plateau), and drops `def_level_pct` from
    drift eligibility (it only decreases), matching the comments with nobody hand-listing a
    channel. **Measured sensitivity:** relaxing `continuous_repeat_max` to 0.5 takes the
    continuous list from 7 to 9 channels and the flag rate from **0.00058 to 0.423**, so
    eligibility is what holds the detector together. A widespread freeze therefore raises a
    channel's baseline repeat rate above the bound and removes it from the list. Fail-safe
    pointing the wrong way. Fix: fit eligibility on a healthy baseline slice and pin it, the
    way imputation medians are already pinned at fit.
  - **T3-4 — the test suite pins shape, not detection: 4 of 5 mutations pass green.** Nine of
    ten tests assert shape, `[0,1]` range, determinism or exception type; the only numeric
    assertion is `test_temporal_does_not_flag_everything` (`< 0.20`) against an actual rate of
    **0.00058** — a **345x** gap. **Measured:** (a) relaxing `DRIFT_MONOTONE_FRAC` from 0.9 to
    0.5 takes the temporal rung from **17 flagged rows at precision 1.000** to **3,180 rows at
    precision 0.050** (precision measured against `is_outlier`, which marks 1,190 rows; the
    looser `anomaly_type != ""` marks 1,387 and reads 0.056) and the **full 205-test suite
    exits 0**; (b) deleting the `-` before
    `self._iforest.score_samples(...)`, which inverts the detector, leaves **10 passed** while
    `joint_outlier` recall at the 2% budget drops **0.104 → 0.000** and top-2% precision is
    **0.049**; (c) `combined = iso` (T3-1) is green; (d) dropping the redundant `& ~np.isnan(v)`
    is green with **byte-identical output** (NaN != NaN already breaks the run — the guard
    cannot be made observable). The one red control was deleting the missing-column `raise` in
    `_as_signal_matrix`: **1 failed, 9 passed**, but the exception that failed the test came
    from sklearn (`ValueError: X has 8 features, but IsolationForest is expecting 9`), not from
    the repo's guard; the temporal path also raises, as a bare `KeyError` from `X[col]` inside
    `fit`. So that guard buys the diagnostic message and the ADR-001 pointer, not the failure
    itself. Fix: the fixture carries `anomaly_type`/`is_outlier` and the scoring harness already
    computes what is missing — assert a per-family recall floor and a precision floor for the
    cheap rungs, plus a flag-rate floor, not just a ceiling.
  - **T3-5 — three comments are wrong, and one of them teaches the wrong concept.** (a) The
    **module docstring still describes the temporal rung that was thrown away**: it defines
    `sensor_stuck` as "rolling variance -> 0" and `sensor_drift` as "a persistent nonzero
    rolling slope", which is exactly the formulation ADR-005 §3 records as failing at F1 ~0.02
    while flagging >85% of rows, and which `TemporalDetector`'s own docstring 200 lines below
    contradicts. The most-read comment in the file teaches the definition the rewrite disproved.
    (b) `DetectionResult`'s docstring promises "per-row suspicion score **plus the column means
    it imputed with**"; the dataclass has `name` and `scores` only. (c) `impute_means_` stores
    `X.median()`, not means. Docs-only, no runtime effect. Fix: rewrite the module docstring's
    two bullets to the shipped signatures (exact-value run / sustained monotone creep) with a
    dated pointer to the rewrite, drop the phantom clause, and rename to `impute_medians_`.

- **Study backlog, queued 2026-09-08 (APROFUNDAMENTOS `R2-T4`, the scoring harness and
  `signal_suspect`):** five findings over `src/pdm_mlops/detect_score.py`,
  `src/pdm_mlops/suspect.py` and their two test files, **none fixed** — the study programme
  documents, it does not repair. Measurement baseline for the scoped suite
  (`tests/test_detect_score.py tests/test_suspect.py tests/test_features.py`): **18 passed**,
  ~12 s. All numbers come from the committed smoke fixture (29,376 rows, 34 units, 2.5 h step,
  **1,190 labelled outlier rows = 4.0509%**), with torch present so the autoencoder rung can be
  scored. Five mutation points were run (the study brake's ceiling) and **four came back green**;
  one control went red. Both source files were restored from a pre-mutation copy afterwards and
  the scoped suite re-run green (`git status --porcelain` clean).
  **T4-1 and T4-2 are the ones that matter** — T4-1 is the project's central guarantee resting on
  a string comparison, T4-2 is a live loss of feature quality. Nothing here is reachable from
  outside the process. ADR-005 is factually strained in two places (see T4-5) but describes what
  the code intends, so no item is 🔴 URGENT under the study brake's narrow valve.
  - **T4-1 — the leakage guard checks column NAMES and never values.**
    `features.assert_no_leakage` is `[c for c in LEAKY_COLUMNS if c in X.columns]`;
    `suspect.add_signal_suspect` calls it on a hand-enumerated view and its docstring claims this
    "proves the feature did not smuggle in a label". **Measured by mutation:** replacing the body
    of `compute_suspect` with `return readings["failure_within_h"].to_numpy().astype(float)` — the
    target itself, under the name `signal_suspect` — leaves the guard silent,
    `features.prepare(suspect_feature=True)` returning normally, and both tests with "leakage" in
    their names (`test_signal_suspect_is_added_and_leakage_safe`,
    `test_suspect_column_is_not_a_label`) **green**; a logistic regression on that frame scores a
    held-out **ROC-AUC of 1.000**. The two reds are the watcher tests, failing for an unrelated
    arithmetic reason. Leaking `is_outlier` instead leaves the scoped suite at **18 passed**, zero
    reds — and the model gets slightly *worse* (test AUC **0.6954** with the leaked column vs
    **0.7000** with no suspect feature at all), because sensor dirt barely predicts failure. That
    second variant is the more instructive one: the guard is equally silent whether the smuggled
    value helps or hurts, so a green leakage test says nothing about either. Fix: add a
    value-side check alongside the name check — no feature column may correlate above ~0.99 with
    any column in `LEAKY_COLUMNS` — and downgrade the docstring's "prove" to "check".
  - **T4-2 — the mean combiner throws away the best rung; the default excludes it entirely.**
    `suspect._combine` averages the rungs and `compute_suspect` defaults to
    `use_autoencoder=False`. **Measured (average precision against `is_outlier`, seed 42):**
    autoencoder alone **0.661**, multivariate **0.218**, temporal **0.054** (chance = the base
    rate, 0.0405); `signal_suspect` as shipped **0.237**, and **0.437** with the autoencoder
    folded in — i.e. **34% below simply using the autoencoder alone**. ADR-005 §6 records that the
    autoencoder "earns its place" while the shipped feature is the mean of the two weakest rungs.
    A simple mean is only sound when components are of comparable quality; here they differ by
    more than 10x in AP. Fix: combine with `max`, or weight by the AP the harness already
    computes, and state the three numbers in the docs so the `[deep]`-extra trade-off is explicit
    rather than implied.
  - **T4-3 — the data-quality watcher cannot see a uniformly bad batch, and raises the wrong
    exception on the worst one.** `data_quality_check` calls `compute_suspect`, which **fits** the
    ladder on the incoming batch and min-maxes within it, so suspicion is a rank inside the batch.
    **Measured** (`baseline_rate = max(fit_baseline_rate(clean_rows), 1e-4)`): on a batch of all
    **28,186** clean rows, freezing `coolant_temp_c` across the whole batch → **does not trip**,
    and multiplying all nine signals by 1000 → **does not trip**. Freezing all nine raises a raw
    sklearn `ValueError` ("The covariance matrix of the support data is equal to 0"), **not**
    `DataQualitySpike`, so a caller following the docstring's `except DataQualitySpike` misses the
    worst possible batch. **The verdict also depends on batch size rather than on the corruption:**
    the same two mutations on a **2,000-row** clean slice both *do* trip — in every one of those
    four runs the ladder flags the same **2 rows**, so what changes is the denominator
    (2/2,000 = 0.001 > 3x the 1e-4 floor; 2/28,186 = 7e-05 < it), not the data quality. A watcher
    whose answer moves with how many rows you hand it is not measuring the batch. The repo's own test passes because
    `readings[readings["is_outlier"]]` is a *mixed* batch by construction. Related: the 0.5 flag
    threshold over a mean is near-unreachable — the fixture's max `signal_suspect` is **0.9743**
    and only **19 of 29,376 rows** reach 0.5, so `fit_baseline_rate` on the clean rows returns
    **7.0957e-05**, a baseline of **2 rows**; both tests paper over this with
    `max(fit_baseline_rate(normal), 1e-4)`, i.e. the test carries a guard the production code
    lacks. Fix: fit the ladder once on a healthy reference window, persist it, and only
    `transform` incoming batches (this is the same fix as T3-2/T3-3 and T4-4); wrap the fit so
    degenerate batches raise `DataQualitySpike`; floor the baseline in the code, not the test.
  - **T4-4 — `signal_suspect` is computed before the train/test split.** `features.prepare` builds
    the column over the full frame and calls `GroupShuffleSplit` afterwards, so the
    IsolationForest, the robust covariance and the min-max all see the held-out units. **Measured:**
    all **9 of 9 test units** are seen by the ladder fit; **8,080 of 21,600 training rows (37%)**
    change by more than 0.01 when the ladder is fit on the training rows only, max shift **0.0700**.
    Not label leakage — the ladder reads no labels — but train-test contamination that the
    (correct) group split does not protect against, so any reported test metric on a
    `suspect_feature=True` frame is optimistic by an unmeasured margin. Fix: express the ladder as
    a fit/transform estimator inside a `Pipeline` so it is fit per fold.
  - **T4-5 — the report and the policy can disagree with the suite fully green.** (a) The caption
    `"(family columns = recall at a fixed top-2% alarm budget)"` is a string literal: raising
    `ALARM_BUDGET` from 0.02 to 0.20 moves `joint_outlier` recall **0.10 → 0.64**, `drift`
    **0.06 → 0.55** and `obvious` **0.38 → 0.94** while the caption still says 2%, at **18
    passed**. (b) Flipping `ae_earns = ae_subtle > best_cheap_subtle` to `<` makes the report
    contradict the numbers printed beside it, at **18 passed** — the offline suite runs without
    torch, so ADR-005 §6's outward-facing claim has no verifier. (c) Loosening
    `SUSPECT_RATE_SPIKE_FACTOR` from 3.0 to **16.0** stays green (red only at 17.0), so the
    watcher can be made **5.3x blinder** unnoticed. (d) The `ALARM_BUDGET` comment says "2% ≈ the
    planted-outlier base rate"; the measured rate is **4.0509%**, which imposes an unstated
    **recall ceiling of 49.41%** (588 slots / 1,190 outlier rows) on every number in the table.
    The one red control was removing tie-awareness in `_alarm_set` (`scores > kth` → `>=`):
    **1 failed, 17 passed** on `test_alarm_set_is_tie_aware_for_sparse_scores` — the mutation the
    suite does catch. Fix: derive the caption from the `budget` argument;
    assert the AE verdict in a torch-marked test; assert a recall floor per family; correct the
    base-rate comment and print the ceiling next to the table.
  - **Also, not a defect but an undeclared limit:** `can_frame_corrupt` (89 rows) and
    `can_frame_stale` (63) carry `is_outlier=True` and so count in ROC-AUC/AP, but appear in **no
    column** of the recall table, because neither is listed in `OBVIOUS_FAMILIES` nor
    `SUBTLE_FAMILIES`. A new generator family would vanish from the detail silently.

- **Study backlog, queued 2026-09-08 (APROFUNDAMENTOS `R2-T5`, grouped HPO, diagnostics-as-artifact
  and the training watchers):** six findings over `src/pdm_mlops/tune.py`,
  `src/pdm_mlops/diagnostics.py` and their two test files, **none fixed** — the study programme
  documents, it does not repair. Measurement baseline for the scoped suite
  (`tests/test_tune.py tests/test_diagnostics.py tests/test_train.py`, optuna and matplotlib both
  installed): **19 passed**, ~245 s. All numbers come from the committed smoke fixture with
  `FIXTURE_SEED = 0` and `suspect_feature=True`: 21,600 training rows over **25 units**, 7,776 test
  rows over 9 units, positive rate 5.282% / 3.819%, `min(N_SPLITS, 25) = 5` folds of 5 units each,
  all class-rich. Five mutation points were run (the study brake's ceiling) and **three came back
  green**; the two reds are described below. Both source files were reverted with
  `git checkout -- src/pdm_mlops` after each run and `git status --porcelain` was **empty** at the
  end of the campaign. **T5-1 and T5-2 are the ones that matter** — T5-1 is the phase's central
  guarantee with no test that executes it, T5-2 is an artifact that describes a different model
  than the one it is attached to. Nothing here is reachable from outside the process and nothing
  makes the README or the live demo state a falsehood today, so no item is 🔴 URGENT under the
  study brake's narrow valve.
  - **T5-1 — the test named after the grouped-CV guarantee never calls the grouped CV.**
    `test_grouped_cv_never_shares_a_unit_across_folds` **constructs its own `GroupKFold`** and
    asserts that scikit-learn behaves as documented; it never invokes `tune._grouped_cv_auc`, and
    its only coupling to this repo is the `N_SPLITS` constant. **Measured by mutation:** replacing
    `GroupKFold(n_splits=n_splits)` with `KFold(n_splits=n_splits, shuffle=True, random_state=0)`
    and `cv.split(X, y, groups)` with `cv.split(X, y)` — i.e. deleting the ADR-003 guarantee
    *inside the search* — leaves **all six tests in `test_tune.py` green**; the scoped suite goes
    to **1 failed, 18 passed**, and the single red is
    `test_diagnostics.py::test_real_fixture_fit_trips_overfit_by_design`. It fails for an unrelated
    reason: leaked folds raise the CV score from **0.7605 to 0.8641** (same model, same data,
    measured directly), the **train−CV** overfit gap drops from **0.224** to ~0.12, below the 0.15
    limit (the train−test gap is a different number, 0.272, and is not what the watcher uses), and
    the watcher stops firing. So the failure message says *"the model stopped overfitting"*, which
    points a reader at the watcher — and the natural "fix" would be to loosen the threshold,
    exactly the wrong direction. For scale, the same leak buys the linear model only **+0.0159**
    (0.6961 → 0.7120): what exploits it is tree capacity. Fix: a test that calls the production
    path — monkeypatch `tune.GroupKFold` and assert it was used, or a behavioural version on a
    dataset where unit identity perfectly predicts the label (grouped ≈ 0.5, ungrouped ≈ 1.0).
  - **T5-2 — the learning-curve artifact drops the model's hyper-parameters.**
    `diagnostics._learning_curve` rebuilds the estimator at each point with
    `models.BUILDERS[model.name](seed=int(model.params.get("seed", config.DEFAULT_SEED)))`,
    forwarding **only the seed**, so the curve attached to a **tuned** run — the entire point of
    F2.6 — is a picture of the untuned model. **Measured without any mutation:** a default (deep)
    LightGBM and a shallow regularised one (`num_leaves=15, min_child_samples=100,
    reg_lambda=10.0`), both fitted on the same frame at seed 0, produce **identical curves in all
    eight cells** (`lc_deep == lc_shallow` is `True`): train 0.9981 / 0.9951 / 0.9900 / 0.9845 and
    test 0.6671 / 0.7128 / 0.7379 / 0.7123 at fractions 0.25 / 0.5 / 0.75 / 1.0. The overrides were
    present on the model (`shallow.params` carries all three) — the function does not read them.
    Secondary, same function: `iloc[:k]` takes a **prefix** of a unit-ordered frame, so "25% of
    rows" is really **7 of 25 machines** (measured), which is arguably a better learning curve than
    random rows but is not what `train_fraction` says. Fix: `_clone(model)` — as
    `tune._grouped_cv_auc` already does — or pass the tunable subset of `model.params` as
    `overrides`; and document (or change) the prefix growth.
  - **T5-3 — the diagnostics test checks filenames, never content.**
    `test_log_diagnostics_writes_artifacts` asserts only that four CSV names exist under
    `diagnostics/<model>` in the run. **Measured by mutation:** pointing `log_diagnostics` at the
    training split instead of the held-out one (`ds.X_test` → `ds.X_train`, and `ds.y_test` →
    `ds.y_train` in both the calibration and the threshold-sweep calls) leaves the scoped suite at
    **19 passed**. Calibration measured on data the model has already seen reads *better* than the
    truth — it shows a well-calibrated model precisely when it is not — and the threshold sweep
    suggests an optimistic operating point; diagnostics are the one output here whose consumer is a
    human, so no second number contradicts them. The same blindness covers empty (header-only)
    CSVs. Fix: one discriminating assert — the `count` column of `calibration.csv` must sum to
    `len(ds.y_test)` (7,776), not to the training size (21,600).
  - **T5-4 — the majority-baseline guard's margin can be deleted with zero reds.**
    `beats_majority = test_auc > MAJORITY_AUC + MAJORITY_MARGIN` (i.e. `> 0.505`). **Measured by
    mutation:** `MAJORITY_MARGIN: float = 0.005` → `0.0` leaves the scoped suite at **19 passed**,
    byte-identical to baseline. Both tests that exercise the guard pin `test_auc` at exactly
    **0.50**, and the comparison is a strict `>`, so `0.50 > 0.505` and `0.50 > 0.500` are both
    false — the guard still trips, the asserts still pass, and the number separating them is never
    observed. With the margin gone, a model at test ROC-AUC **0.503** — indistinguishable from
    chance at this sample size — is *approved* by `pdm train --audit`. This is the shape that
    matters: the mutation that **widens a policy by one number** is invisible while the one that
    **deletes a mechanism** (T5-1's control, and the seeded sampler) is caught, and widening is
    what a real pull request looks like. Fix: a boundary test — 0.503 must be rejected, 0.51 must
    pass.
  - **T5-5 — record drift: four statements written in the present tense about things that moved.**
    (a) The `N_SPLITS` comment says *"the fixture's ~15 train units"*; the measured value is **25**
    (the fixture was re-stratified by ADR-019 after the comment was written). (b) `tune()` logs
    `log_param("cv", f"GroupKFold(n_splits={N_SPLITS})")` — the **constant**, always 5 — while the
    value actually used is `min(N_SPLITS, groups.nunique())`; on a dataset with fewer than 5 units
    the run would record a cross-validation that did not happen. This is the only item with
    operational consequence — and it is **by code inspection, not measured**: the committed fixture
    has 25 units, so `min(5, 25) == 5` and no run on it can distinguish the logged value from the
    effective one. (c) `TuneResult.n_trials` (and therefore `log_param("n_trials", …)` and
    `format_tune`) is the **requested budget**, not the number of trials that completed;
    `len(study.trials)` is the real one — also by inspection, since Optuna propagates an objective
    exception rather than silently dropping the trial, so no cheap repro exists. (d) The `tune.py` module docstring states the measured HPO
    delta (+0.003 / 0.000 on data 0.2.0) in the present tense and without a date. Fix: correct the
    unit count, log the effective `n_splits`, record completed trials, and date the docstring's
    measurement.
  - **T5-6 — two silent degradations, neither firing today.** (a) `_grouped_cv_auc` skips
    single-class folds with a bare `continue` and **never records how many it skipped**, so a
    "5-fold" mean can quietly become a one-fold holdout while MLflow still logs
    `GroupKFold(n_splits=5)` and the watcher's overfit gap is computed against it. On the fixture at
    seed 0 all five folds are class-rich (measured: 5 units, 2 classes, 134–322 positives each), so
    this is latent, not active. (b) `_matplotlib()` catches bare `Exception`, so a *broken*
    matplotlib (corrupt font cache, unavailable backend, incompatible version) is indistinguishable
    from an absent one and the PNGs vanish without a warning — and because the only diagnostics test
    checks CSV names (T5-3), the suite stays green. Fix: count and expose skipped folds (and log the
    effective fold count with T5-5b); narrow the catch to `ImportError` and `warnings.warn` on
    anything else.
  - **Also, not a defect but an undeclared limit:** the fold positives on the fixture range from
    **134 to 322** across the five folds (a 2.4x spread), which enters the objective as noise in the
    5-fold mean. `StratifiedGroupKFold` would keep the grouping guarantee and cut that variance at
    no cost; the choice of plain `GroupKFold` is not recorded anywhere as a decision.

- **Study backlog, queued 2026-09-09 (APROFUNDAMENTOS `R2-T6`, the temporal contender — causal
  TCN):** five findings over `src/pdm_mlops/sequence.py` and `tests/test_sequence.py`, **none
  fixed** — the study programme documents, it does not repair. Measurement baseline:
  `pytest tests/test_sequence.py -q` → **10 passed**, ~24 s (notebook, torch 2.12+cu130, CUDA
  available, so no test skipped for the missing `[deep]` extra); the full suite also passes
  (**205 passed**, ~700 s). All numbers below come from the committed smoke fixture at `FIXTURE_SEED = 0` with the
  tiny CPU TCN the tests use (`window=6, channels=4, layers=2, epochs=2`); the reference ladder on
  that fixture is `lightgbm_perrow` **0.7041** / `lightgbm_temporal` **0.6360** / `tcn` **0.5935**
  — toy numbers that measure the contract, never accuracy. Six mutation points were run (the study
  brake's ceiling) and **four came back green**; `src/pdm_mlops/sequence.py` was restored after
  each run and `git status --porcelain` was **empty** at the end of the campaign. **T6-1 and T6-2
  are the ones that matter** — together they are the two halves of the causality guarantee ADR-007
  advertises as *structural*, and neither has a test that goes red when it is deleted. Scope note:
  every number published in ADR-007 was produced by the correct path (shared split, train-only
  scaler, equal window via the CLI); what these findings measure is that **nothing would notice if
  that path stopped being correct**. Nothing here is reachable from outside the process and nothing
  makes the README or the live demo state a falsehood today, so no item is 🔴 URGENT under the
  study brake's narrow valve.
  - **T6-1 — the window-causality assertion cannot go red.**
    `test_windows_are_causal_and_unit_bounded` asserts `(w.win_idx <= s).all()`, but `win_idx` is
    produced by `np.clip(rawpos, us, s)` — the assertion restates the upper bound the clip has just
    imposed, so it holds for any `rawpos` whatsoever. **Measured by mutation:** replacing
    `rawpos = s - window + 1 + k` with `rawpos = s + k` — a window that points *forward* — leaves
    `tests/test_sequence.py` at **9 passed, 1 skipped, 0 failed**. Under that mutation every window
    collapses to the current row repeated (verified directly: `win_idx == s` at every position,
    one distinct entry per window, `win_valid` all ones), i.e. the TCN silently stops being a
    temporal model while still being reported as rung (c) of the ladder. The only signal on screen
    is `test_left_pad_positions_are_zeroed_in_every_channel` turning into a **skip** — there are no
    short-history rows left to inspect — which reads as routine. A second mutation confirms where
    the guarantee actually lives: relaxing the clip's upper bound to `s + 1` is **inert**
    (`10 passed`), because `rawpos` never exceeds `s` by construction (verified: no window entry
    is greater than its current row). Fix: a behavioural test of the same shape as the one that
    already guards rung (b) — corrupt a future row of a unit and assert an earlier row's window
    contents (or score) do not move — and assert on `rawpos` rather than on the post-clip array.
  - **T6-2 — the causal convolution has no test at all.**
    ADR-007 states that causal padding *"structurally forbids intra-window future leakage"*. The
    structure is two lines: `padding=(kernel-1)*dilation` on each `Conv1d` and the right-hand crop
    in `_Chomp.forward`. **Measured by mutation:** replacing that crop with `return x` leaves
    `tests/test_sequence.py` at **10 passed**. Under the mutation the conv stack emits **12**
    timesteps for a 6-step window, so `h[:, :, -1]` — meant to be "the current instant" — becomes a
    position built mostly from right-hand padding, and the fixture ROC-AUC of the tiny TCN *rises*
    from **0.5935 to 0.6563**, so the metric does not flag it either. Within the current pipeline
    the immediate damage is the head reading padding rather than leakage of real future data (each
    batch element is a self-contained window); it becomes leakage the day someone feeds a whole
    unit series at once, which is the obvious way to speed up inference. Fix: an equivalence test —
    the module's output at the last position must be unchanged when timesteps after the window's
    end are perturbed — or an explicit shape assertion that the stack's output length equals the
    window length.
  - **T6-3 — nothing prevents the scaler from seeing the test rows.**
    `build_windows` standardises with `fit_rows = raw if train_idx is None else raw[train_idx]`,
    documented as an offline convenience. **Measured by mutation:** `fit_rows = raw` — the scaler
    fitted on every row, test rows included — leaves `tests/test_sequence.py` at **10 passed**.
    `TCNClassifier.fit` does pass `train_idx` today, so the reported numbers are unaffected; what
    is missing is any guard that would notice if it stopped. The magnitude of the resulting
    contamination would be small (mean/std over hundreds of thousands of rows barely move with 25%
    more data), which is precisely what makes it hard to spot while it poisons the honesty claim
    the ladder exists to support. Fix: make `train_idx` required for the fitting path (keep the
    `None` convenience only behind an explicit flag), and assert that the standardisation stats
    computed with and without the test rows differ on a fixture where they must.
  - **T6-4 — the verdict is tested for internal consistency, not for policy, and its resolution is
    accidental.** `test_compare_runs_three_rungs_on_same_test_rows` asserts
    `cmp.tcn_earns_its_place == (tcn > temporal)`, which ties the boolean to *a* comparison but not
    to *the* comparison. **Measured by mutation:** relaxing `tcn_earns = tcn_metric >
    temporal_metric` to `> temporal_metric - 0.01` leaves the file at **10 passed**; only at
    `- 0.05` does it go to **1 failed, 9 passed**. The reason is arithmetic: the fixture margin is
    **−0.0425** (tcn 0.5935 vs temporal 0.6360), so a tolerance smaller than that leaves both sides
    of the equality `False`. The published margin on the full data is **0.0046** — ten times
    smaller — so a 0.01 tolerance there would invert ADR-007's headline verdict while this suite,
    which runs on the fixture, stayed green. (That 0.0046 is ADR-007's published figure, not
    something reproduced here; ADR-010 already records the same-geometry TCN rung reproducing at
    **0.7979** on torch 2.12+cu130 versus ADR-007's 0.8148 — a cross-CUDA numerics gap that widens
    the margin rather than closing it.) Fix: assert the policy itself (strict inequality, no
    tolerance) against synthesised metric pairs, independently of whatever the fixture happens to
    produce.
  - **T6-5 — `compare` force-syncs an injected contender's seed but not its window.** In the
    `else` branch, `tcn.seed = seed` is applied and `tcn.window` is left alone, so an injected
    `TCNClassifier(window=6)` competes against a rung (b) built with the `window` argument (24 by
    default). That is exactly what the test suite does. **Measured:** on the fixture, rung (b)
    scores **0.6360** at window 24 and **0.6865** at window 6 — a 0.0505 spread, an order of
    magnitude larger than the 0.0046 margin the published verdict turns on. `pdm sequence` passes
    the same window to both, so no published number is affected; the library contract is what
    allows the divergence. Fix: set `tcn.window = window` alongside the seed, or raise when an
    injected contender's window disagrees with the argument.

- **Study backlog, queued 2026-09-09 (APROFUNDAMENTOS `R2-T7`, characterizing the ceiling):** six
  findings over `src/pdm_mlops/ceiling.py` and `tests/test_ceiling.py`, **none fixed** — the study
  programme documents, it does not repair. Measurement baseline: `pytest tests/test_ceiling.py -q`
  → **13 passed**, ~62 s (notebook). All numbers below come from the committed smoke fixture
  (29,376 rows, 34 units, 1,438 positives) at `FIXTURE_SEED = 0`, `window=6`; the reference report
  on that fixture is honest per-row **0.7041**, `lightgbm_temporal` **0.6865**, stack **0.7065**
  (margin **+0.0024**), fenced bound **1.0000** (gap **+0.2959**), `ceiling_is_data = False`.
  Six mutation points were run (the study brake's ceiling) and **five came back green**;
  `src/pdm_mlops/ceiling.py` was restored after each run and `git status --porcelain` was **empty**
  at the end of the campaign. **T7-2, T7-3 and T7-4 are the ones that matter**: each drives the
  capstone's headline flag to `ceiling_is_data = True` — i.e. towards *confirming* the thesis the
  module exists to test — with the suite fully green. Scope note: every number published in
  ADR-010 was produced by the correct path (clean honest frames, grouped inner folds, a bound that
  really leaks); what these findings measure is that **almost nothing would notice if that path
  stopped being correct**. **Public-surface note:** `characterize` + `format_report` are reachable
  by a user — they back the shipped `pdm ceiling` subcommand (`cli.py`, advertised in the README) —
  so T7-3's failure mode (a report printing *"leaks failure_mode, time_to_failure_h"* while leaking
  nothing) would be visible to anyone running the artifact, not merely internal. The HTTP API and
  the live demo are unaffected (`serve.py` never imports `ceiling`). On the unmutated tree the
  report is truthful and reproduces the numbers the README narrates, so nothing states a falsehood
  today and no item is 🔴 URGENT under the study brake's narrow valve.
  - **T7-1 — the honest-path fence has no test at its call site.**
    `test_base_frames_are_leak_free` calls `ceiling._assert_honest_frame` itself and
    `test_honest_frame_rejects_leak_features` hands the function a hand-built bad frame: both prove
    the *function* works, neither proves `build_base` *calls* it. **Measured by mutation:**
    commenting out the two `_assert_honest_frame(...)` calls in `build_base` leaves
    `tests/test_ceiling.py` at **13 passed** (report byte-identical to baseline). The layer that
    would go missing is the one covering `time_to_failure_h` — a column this module derives, and
    **measured**, `features.LEAKY_COLUMNS` upstream is
    `('failure_within_h', 'failure_mode', 'anomaly_type', 'is_outlier')`, which does not list it.
    Fix: a behavioural test — hand `build_base` a `readings` frame whose feature path would carry a
    forbidden column and assert it raises.
  - **T7-2 — both leakage guards are name-based, and an innocently-named leak makes the whole
    capstone self-confirm.** `features.assert_no_leakage` and `ceiling.LEAK_FEATURES` walk lists of
    column *names*; a column derived from the target under a telemetry-sounding name passes both.
    **Measured by mutation:** adding `X_perrow["load_index"] = readings[config.TARGET]` inside
    `build_base`, with both assertions left in place, leaves the file at **13 passed** and the
    fixture report becomes honest **1.0000**, bound gap **0.0000**, stacking margin **0.0000**,
    `ceiling_is_data = True`. All three instruments converge on "the honest model is exactly at the
    information ceiling, the rungs are redundant, thesis confirmed" — the strongest claim the
    report can make — produced by total label leakage with no test red. The convergence is not
    independent: the three readings share one input frame. Fix: provenance rather than names —
    build the honest matrix only through `features.select_features` (the allowlist that actually
    holds) and forbid post-selection column assignment on the honest path.
  - **T7-3 — nothing asserts that the upper bound actually leaks.**
    `test_upper_bound_bounds_and_is_at_least_honest` asserts `ub.leaky >= ub.honest - 1e-9`, and a
    tie satisfies it. **Measured by mutation:** deleting the two leaky-column assignments in
    `upper_bound` leaves the file at **13 passed**, with `honest = leaky = 0.7041` and
    `gap = 0.0`. Worse, `UpperBound.leak_features` is the module constant rather than a description
    of the frame that was fitted, so `format_report` still prints
    `upper-bound (DIAGNOSTIC, leaks failure_mode, time_to_failure_h): 0.7041` — declaring a leak
    that did not happen. By the `UpperBound` docstring's own reading, `gap = 0` means "the honest
    model is at the ceiling", so a switched-off instrument reports the thesis confirmed. Fix:
    assert `leaky > honest` with slack on a fixture where the bound must win, and derive
    `leak_features` from the columns actually added.
  - **T7-4 — the inner fold's grouping guarantee has no test, and dropping it inverts the published
    verdict.** `_oof_predictions` uses `GroupKFold` and its docstring cites ADR-003, but no test
    observes fold composition. **Measured by mutation:** swapping it for
    `KFold(n_splits=n_splits, shuffle=True, random_state=seed)` leaves the file at **13 passed,
    12 warnings** — `UserWarning: The groups parameter is ignored by KFold` was the only distinct
    warning class captured, and it is the whole of the on-screen signal — while the fixture margin
    moves from **+0.0024 to −0.0017**, flipping `beats_best_base` to
    `False` and `ceiling_is_data` to `True`. That is ADR-010's central finding (the refutation)
    silently inverted. Note the direction: row-wise folds make both rungs' OOF predictions
    optimistic in similar ways, so the leak *flattens* the comparison rather than inflating it.
    Fix: assert unit-disjointness across the inner folds the same way
    `test_base_split_is_the_exact_f1_split` asserts it for the outer split.
  - **T7-5 — the thesis flag reads one instrument while the write-up says three converge.**
    `CeilingReport.ceiling_is_data` is `not self.stacking.beats_best_base`; the decomposition and
    the bound do not enter it, although ADR-010 and the module docstring describe three converging
    instruments. The margin it turns on is **+0.0073** (two-rung, full data) from a single
    deterministic OOF meta-learner with no confidence interval — a limitation ADR-010 already
    records in prose, but the boolean does not. Fix: either report a spread over repeated
    seeds/folds, or have the property abstain (tri-state) when the margin is inside the measured
    run-to-run variation.
  - **T7-6 — the honest probability is fitted twice, so consistency between two report fields is a
    coincidence.** `decompose`, `upper_bound`'s honest fit and the stacking probe's
    `lightgbm_perrow` rung all call `_fit_predict` with identical arguments (same frame, y,
    train/test indices and seed) — **measured by fingerprinting the four `_fit_predict` calls of one
    `characterize` run: only two distinct fingerprints, the per-row fit repeated three times** —
    wasted compute on the 3.47M-row runs. And because the value is
    recomputed rather than shared, nothing ties `Decomposition.overall` to `UpperBound.honest`
    despite the `UpperBound` docstring stating they are the same number. **Measured by mutation:**
    pointing `decompose` at `base.X_temporal` leaves the file at **13 passed** with
    `overall = 0.6865` and `honest = 0.7041` — two fields of one report describing different
    models, printed as if consistent. Fix: fit once in `characterize` and pass the honest
    probability into both instruments.
- **Study backlog, queued 2026-09-09 (APROFUNDAMENTOS `R2-T8`, the governed registry — gated
  promotion + rollback):** five findings over `src/pdm_mlops/registry.py` and
  `tests/test_registry.py`, **none fixed** — the study programme documents, it does not repair.
  Measurement baseline: `python -m pytest tests/test_registry.py -q -p no:randomly` → **14 passed**,
  ~35 s (notebook). Five mutation points were run (under the study brake's ceiling of six) and
  **four came back green**; `src/pdm_mlops/registry.py` was restored after each round and
  `git diff --stat` on it was **empty** at the end of the campaign. **T8-1 is the only one
  reachable without mutating anything** — a shipped CLI command tracebacks — and **T8-3 is the most
  dangerous** — it turns `rollback` into a no-op that reports success. Scope note: the gate itself
  decided correctly in every case measured (the worse candidate never promoted, the alias never
  moved when it shouldn't, rollback restored the right version on the unmutated tree); what these
  findings measure is how much of that could stop being true with no test going red. The HTTP
  surface looks unaffected by T8-1: `serve.py` uses `registry._client`, `production_version`,
  `PRODUCTION_ALIAS` and `version_metric` (all four exercised by the passing `tests/test_serve.py`),
  and the three write-side names — `promote`, `rollback`, `format_promotion` — do not appear in it.
  That last part is a **static grep**, not an execution result: no test drives serving into the
  crashing report path, so "serving cannot reach T8-1" is read from the source, not measured.
  - **T8-1 — 🔴 URGENT: `format_promotion` raises `TypeError` when the candidate is already the
    production version, and `pdm promote` run twice is enough to hit it.** The `inc` branch tests
    `result.incumbent_version is not None` and then formats `result.incumbent_metric` with `:.4f`.
    `promote` deliberately skips reading the incumbent's metric when the candidate *is* the
    incumbent (`incumbent != version` in the read condition), so it legitimately returns
    `incumbent_version='2'` with `incumbent_metric=None`. **Measured:** promote v2, then
    `registry.promote(client, NAME, v2)` again → `PromotionResult(..., incumbent_metric=None,
    promoted=True, reason='already the production version')`, and `registry.format_promotion(...)`
    on it raises `TypeError: unsupported format string passed to NoneType.__format__`. Reachable
    from the advertised CLI: `pdm promote` without `--version` resolves `latest_version`
    (`cli.py:265`) and prints `format_promotion(promotion)` unconditionally (`cli.py:275`), and
    `main()` has **no `try`/`except` at all**, so the user gets a traceback. The decision is correct
    and the alias does not move — only the report crashes (measured: the alias still points at the
    same version after the `TypeError`). `flows.py` promotes the version `train` has just registered
    (`flows.py:129-153`), so the "candidate is the incumbent" state should not arise there — **read,
    not executed**: no probe drove the flow into that state. Fix: test `incumbent_metric is not None`, or make
    the version/metric pair unconstructible in `PromotionResult`.
  - **T8-2 — the default gate tolerance has no test.** `test_tie_promotes_by_default` pins the tie
    rule and `test_min_delta_tolerates_a_small_regression` pins an *explicit* `min_delta=0.01`;
    nothing asserts that `DEFAULT_MIN_DELTA` is `0.0`. **Measured by mutation:** changing it to
    `0.004` leaves the file at **14 passed** — it slips under the regression test, which uses a
    0.005 gap. The module's headline guarantee silently weakens from "no worse model promotes" to
    "no model more than 0.004 worse promotes"; for scale, 0.004 is about half the +0.0069 the
    temporal-features rung earned in ADR-007. Fix: assert the constant, and add a default-path
    rejection test at a gap smaller than 0.005.
  - **T8-3 — the boundary `str(version)` normalisation has no test, and without it `rollback`
    becomes a no-op that reports success.** Both sides normalise (module L176, test helper L60) and
    neither asserts it; the whole suite passes `str`. **Measured by mutation:** deleting
    `version = str(version)` leaves the file at **14 passed**; with it deleted, calling
    `registry.promote(client, NAME, int(v2))` while v2 is production yields `promoted=True`,
    `candidate_version=2`, `incumbent_version='2'` (because `'2' != 2`), writes
    `superseded_production_version = '2'` **onto v2 itself**, and from then on `registry.rollback`
    returns `2`, leaves the alias where it was, and the CLI prints "Rolled back: production is now
    v2". The emergency lever silently stops working. Fix: a boundary test that promotes with an
    `int` version and asserts the tag and the alias.
  - **T8-4 — the deleted-predecessor guard in `rollback` has no test, and what it buys is the error
    type, not safety.** `_get_version(client, name, prev)` carries its own comment ("Verify the
    predecessor still exists…") and no test deletes a version. **Measured by mutation:** removing it
    leaves the file at **14 passed**; building the case by hand (promote v1, promote v2,
    `client.delete_model_version(NAME, v1)`, `rollback`) gives `PromotionError: model '…' version 1
    not found in the registry` **with** the guard and `MlflowException: Model Version (name=…,
    version=1) not found` **without** it. The alias moves in neither case — the backend refuses too.
    So the guard upholds the documented `Raises: PromotionError` contract, and without it a library
    exception reaches a CLI that has no handler. Fix: the test that deletes the predecessor.
  - **T8-5 — the idempotent `incumbent == version` branch has no test, and removing it exposes a
    misleading diagnostic.** **Measured by mutation:** deleting the branch leaves the file at
    **14 passed**, and re-promoting the production version then raises `PromotionError: incumbent v1
    of '…' has no comparable roc_auc; the gate has nothing to compare against`. Two readings: the
    branch the source comment marks *unreachable* (L191) does fire as designed when the branch above
    it is removed — a readable error instead of `None - float`, which is the empirical case for
    encoding impossible states — but its **message is wrong**: the incumbent has a metric; the code
    chose not to read it. Fix: an idempotence test (`promote` the current production version →
    `promoted=True`, alias unchanged, no tag written) and a message that says the metric was not
    read rather than not logged.
- **Study backlog, queued 2026-09-09 (APROFUNDAMENTOS `R2-T9`, serving through the alias — the
  contract between governance and HTTP):** five findings over `src/pdm_mlops/serve.py` L1–527 and
  `tests/test_serve.py`, **none fixed** — the study programme documents, it does not repair.
  Measurement baseline: `python -m pytest tests/test_serve.py -q` → **11 passed**, ~77 s (notebook).
  Five mutation points were run (under the study brake's ceiling of six) and **three came back
  green**; `src/pdm_mlops/serve.py` was restored after each round and `git diff --stat` on it was
  **empty** at the end of the campaign. **T9-2 is the most dangerous**: the endpoint can serve
  `1 - p` with the whole file green. Scope
  note: on the unmutated tree every measured behaviour was correct — the promoted version is the one
  served, `/health` distinguishes up from ready, `/predict` and `/model-info` answer 503 with nothing
  promoted, and a fresh `ModelStore` follows a rollback. What these findings measure is how much of
  that could stop being true with no test going red.
  - **T9-1 — the model cache is never invalidated in production: a promotion or rollback only
    reaches a running process at restart.** `ModelStore.clear()` exists and works, and the module
    docstring (`serve.py:32`) says a rollback "is picked up by clearing the cache". **Measured:**
    grepping for call sites (`\.clear()`) across the repository, `.venv` excluded, returns a single
    caller — `tests/test_generate_api.py:303`. No endpoint, no job, no lifespan hook calls it.
    `test_store_clear_repoints_after_rollback` does not cover this: it constructs a **new**
    `ModelStore` on its penultimate line, so it proves a process starting *now* sees the restored
    version, not that a serving process does. **Confirmed by probe:** a live store loaded on v2 keeps
    answering `version == "2"` after a rollback and only reports `"1"` after an explicit `clear()`,
    while a freshly built store reports `"1"` immediately. Impact: an emergency rollback does
    not take the bad model out of a live instance until the container restarts. "No redeploy" stays
    literally true (no image, no config changes) and operationally weaker than the docstring reads.
    Fix: a TTL on the cached entry (bounded staleness, no cross-replica coordination needed), and
    reword the docstring to describe what is wired rather than what is possible.
  - **T9-2 — the positive-class column has no oracle: serving `1 - p` leaves the whole file green.**
    `_load_predict_proba` returns `proba[:, 1]`. **Measured by mutation:** changing it to
    `proba[:, 0]` leaves the file at **11 passed**. The only assertion on the values is
    `all(0.0 <= p <= 1.0 for p in probs)`, and the complement is in range too. The service would
    invert its product — the machine closest to failure gets the lowest probability — with nothing
    red anywhere in the pipeline. Fix: assert **meaning**, not range: score the same rows through
    `models.Model.predict_proba` and require equality, or assert ordering against a known-bad row.
  - **T9-3 — an unknown signal key is silently dropped and answered with a confident 200.** The
    request schema is `dict[str, float | None]` and `_to_frame` reindexes to
    `features.FEATURE_COLUMNS`, so a caller who sends `egt_celsius` instead of `egt_c` has that
    column dropped and the real signal scored as era-NULL missing, with nothing in the response
    saying so. **Measured by request:** posting `egt_c_TYPO` in place of `egt_c` returns **200** with
    a body carrying exactly `failure_probability`, `model_version`, `n_rows` — no mention of the
    dropped key. Note there is **no precedent to copy** for the fix: the F8 upload path's
    `unmapped_signals` lists the expected signals that were *missing*, not supplied keys that were
    *ignored* — the opposite direction. Fix: echo ignored keys back in `PredictResponse`, or reject
    unknown keys behind a strict flag; either way it is new behaviour, not an existing pattern
    applied to one more endpoint.
  - **T9-4 — `_is_demo_version` swallows every exception and errs toward *dropping* the honesty
    label.** Its `except Exception: return False` means "not the demo model", so under a registry
    hiccup the public demo stops labelling a fixture-trained number as a demo — the one claim this
    repository works hardest to keep attached. `/model-info` performs the same lookup **without** a
    try/except (`serve.py:450`), so the two surfaces disagree under failure: one errors, the other
    asserts a full-data model. **Measured by fault injection:** making the client's
    `get_model_version` raise `RuntimeError` yields `/predict` → **200 with `demo=False`** and
    `/model-info` → **500** in the same process. Fix: return `True` (or a third "unknown" state) on
    the degraded path, or log and propagate.
  - **T9-5 — only the schema's `min_length=1` stands between an empty batch and a 500.** **Measured
    by mutation:** removing `min_length=1` turns `test_empty_readings_is_rejected` red — but with
    `ValueError: Input data must be 2 dimensional and non empty` raised inside `lightgbm/basic.py`,
    i.e. the empty body traverses `_to_frame` and `_score_frame` and detonates inside the model,
    which is a 500 in the running app. Nothing on the scoring path checks that the frame has rows.
    Related, in the opposite direction: the three copies of `features.assert_no_leakage` on the
    scoring paths (`serve.py:358`, `serve.py:481`, `upload.py:225`) all **execute on every request
    and can never fail** — `_to_frame` reindexes to the nine feature columns and
    `upload.build_frame` populates only those nine slots, so no label column can exist by the time
    the guard runs. **Measured:** deleting both copies in `serve.py` leaves
    `tests/test_serve.py tests/test_upload.py` at **36 passed**; the `upload.py` one was not
    mutation-tested — that copy is argued structurally only. The module has guards where nothing
    can go wrong and none where something can. Fix: a row-count check in `_score_frame`; and either
    delete the non-firing guards or move one to the boundary where a leaky column could actually
    enter.


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
