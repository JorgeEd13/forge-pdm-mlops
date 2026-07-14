# Deploy — the hosted free-tier `/health` link (F6)

The F6 stretch goal: a **reachable live endpoint** so the serving layer isn't just a
`docker compose up` claim but a URL a reviewer can click. Target: **Hugging Face Spaces**
(a Docker Space) — a permanent free URL with no idle cold-sleep, and it fits the
ML-portfolio narrative.

## What makes the live link meaningful

A fresh cloud deploy starts with an **empty** MLflow registry, so `/health` would honestly
report `model_loaded=false` and `/predict` would 503 — a weak showcase. So the deploy image
([`Dockerfile.hf`](../Dockerfile.hf)) **bakes a demo registry at build time**
([`scripts/seed_demo_registry.py`](../scripts/seed_demo_registry.py)): it trains on the
committed smoke fixture, registers the winner, and promotes it to the `production` alias
through the **same F3 gate** as any other model. The endpoint then answers a real prediction
the moment it boots.

**Honest-status boundary (ADR-001 / ADR-014).** The baked model is trained on the *smoke
fixture*, not the full dataset — the build runs offline on a free runner with no generator and
no network. This is allowed because it is a **demo model for the live endpoint, not a reported
metric**: ADR-001 forbids *reporting* a fixture-scored number, not *serving* a demo to prove
the endpoint is wired. Every surface says so — the run/version is tagged `demo=fixture`,
`/model-info` exposes the provenance, and the README's live-link line calls it a demo. The
real ≈0.82 ROC-AUC model is the one `pdm train` produces on the full data locally, and that is
the only number ever quoted.

## The one portability constraint

MLflow stores artifact locations as **absolute paths** in the tracking DB. So the store is
relocatable only if the **build path equals the run path**. `Dockerfile.hf` fixes that path at
`/mlflow` for both, so the baked DB's artifact URIs resolve at serve time. Don't change the
store path in only one place.

## Deploy to Hugging Face Spaces

1. **Create a Space** (owner `JorgeEd`, name `forge-pdm-mlops`) → SDK **Docker**, hardware
   **CPU basic (free)**, visibility **Public**. Set the **short description** (Settings) to:
   `Live serving demo for the forge-pdm-mlops pipeline` (HF truncates it hard — this fits).
2. HF Spaces reads its config from the **Space README front-matter** (not this repo's
   README). Paste this header into the Space's `README.md`:

   ```yaml
   ---
   title: forge-pdm-mlops serving
   emoji: 🔧
   colorFrom: blue
   colorTo: indigo
   sdk: docker
   dockerfile_path: Dockerfile.hf
   app_port: 8000
   pinned: false
   ---
   ```

   `dockerfile_path` points HF at the self-contained image; `app_port: 8000` matches the port
   the app (and the Dockerfile `EXPOSE`/`CMD`) listens on.

3. **Push the repo to the Space** (a Space is a git remote):

   ```bash
   git remote add space https://huggingface.co/spaces/JorgeEd/forge-pdm-mlops
   git push space main
   ```

   HF builds `Dockerfile.hf` on its runners (the fixture-registry bake happens there — no
   secrets, no network).

4. Once the build is green, the Space serves at
   `https://JorgeEd-forge-pdm-mlops.hf.space`. Verify:

   ```bash
   curl -s https://JorgeEd-forge-pdm-mlops.hf.space/health
   # {"status":"ok","model_loaded":true,"model_version":"1"}
   curl -s https://JorgeEd-forge-pdm-mlops.hf.space/model-info
   # {"registered_model":"...","production_version":"1","primary_metric":"roc_auc",...}
   ```

5. **Put the live link in the README** (the F6 DoD): a reachable `/health` badge/line.

## Build & smoke-test the image locally (optional, needs Docker)

```bash
docker build -f Dockerfile.hf -t forge-pdm-mlops:hf .
docker run --rm -p 8000:8000 forge-pdm-mlops:hf &
# The demo model is baked at STARTUP (scripts/hf_entrypoint.sh), so give it ~1–2 min
# on first boot (it trains the two contenders on the fixture and promotes the winner)
# before the first request:
sleep 90
curl -s localhost:8000/health      # {"status":"ok","model_loaded":true,"model_version":"1"}
curl -s -X POST localhost:8000/predict \
  -H 'content-type: application/json' \
  -d '{"readings":[{"coolant_temp_c":95,"engine_load_pct":80}]}'
```

**Why the bake runs at startup, not at build.** MLflow writes **absolute** artifact paths
into the registry DB, and on HF the fixture is an LFS object only smudged into a real file
in the running container — so a build-time bake can promote a model whose artifacts the
runtime user (UID 1000) can't resolve, and `/health` then reports `model_loaded=false`. The
entrypoint bakes as the runtime user, at the runtime path, after LFS smudge — and fails
loud if the fixture is still a pointer. The bake itself (train + promote on the fixture) is
verified end-to-end natively: a fresh serving process over the baked store returns
`model_loaded=true`, a real `/predict` probability, and `/model-info`.

## Managed cloud: Google Cloud Run + managed Postgres (F7 — the managed-cloud gate)

HF Spaces (above) is *free-tier hosting*; **F7** is the stronger, separate claim — operating
a **managed cloud runtime** (Cloud Run) with a **managed resource** (a managed Postgres) in
production. It runs the **same** `Dockerfile.hf` image (already `$PORT`-aware via
`hf_entrypoint.sh`, which bakes the demo registry at startup), adds an interactive
**demo UI** (`/demo`), and **logs each served prediction to the managed Postgres** — the state
that gives the managed database an honest job. See ADR-015 for the rationale.

**Two backends, one gate.** The prediction log (`store_pg.open_log`) takes **any** SQLAlchemy
URL, so the managed resource is a config choice, not a code change:

- **Neon (free, the reference deploy)** — a serverless Postgres with a real free tier
  (scale-to-zero, no card). Cloud Run's own free tier + Neon = the whole gate at **$0/mo**.
  Use `scripts/deploy_cloudrun_neon.sh`.
- **Cloud SQL (paid)** — GCP-native Postgres over a `/cloudsql/<connection>` unix socket. No
  free tier (~$8–10/mo while it exists). Use `scripts/deploy_cloudrun.sh`.

Both build the same image via **Cloud Build** (no local Docker daemon), store the
`DATABASE_URL` in **Secret Manager** (never printed, never committed), and roll a Cloud Run
revision. The image installs `.[serve,cloud]`, so the `psycopg` driver is present for the
managed-Postgres path (dormant on HF, where `DATABASE_URL` is unset).

**The demo UI.** `GET /demo` is a self-contained "set the J1939 parameters → get the failure
probability" page (same `demo=fixture` honesty banner as `/model-info`); `POST /demo/predict`
scores *and* logs. The log stores only the synthetic signal values + the probability + the
model version + a UTC timestamp — **no PII**. When `DATABASE_URL` is unset (local, HF, CI)
the demo degrades gracefully to **no persistence** — nothing about serving depends on the DB.

**Bring-your-own-data (F8, ADR-017).** `/demo` also takes a **CSV/Parquet upload** →
`POST /demo/upload` scores every row and returns per-row probabilities + a summary. Because a
real batch won't use our nine exact header names, the page runs a **map-your-columns step**:
the upload is fuzzy-matched (stdlib `difflib` + a J1939 synonym table — no new dependency) and
the tester confirms/corrects the mapping before scoring; any unmapped signal is scored as
era-`NULL`, so a **partial** dataset still scores ("N of 9 provided"). Bounded by a 2 MB / 5k-row
cap; a non-J1939 or unparseable file is a clear 4xx, never a 500. **No uploaded row is stored** —
the managed-DB posture stays "counts/summaries, never raw uploaded rows"; F8 writes nothing.

**Generate-your-own-data (F14a, ADR-026) — the deploy grows a SECOND unit.** `/demo` can also
**generate** a bounded synthetic fleet and score every vehicle in it. The important part is not the
feature, it is the **shape**: generation runs in a **Cloud Run Job** — its own image
(`Dockerfile.worker`, `.[cloud,generate]`), its own lifecycle, its own scaling — and *not* in a
FastAPI `BackgroundTask` on the serving container (decision **S2**). The API only enqueues: it writes
a `queued` run to Postgres, starts a job execution over the Cloud Run Admin API with the run's
parameters as env overrides, and answers **202 in ~16 ms**; the page polls, then browses the stored
fleet and shows the per-vehicle risk roll-up.

So the deploy is now **two images and two Cloud Run resources**:

| unit | image | extras | shape |
|---|---|---|---|
| API | `Dockerfile.hf` | `[serve,cloud]` | Cloud Run **service** — long-lived, request-scoped, warm |
| generation worker | `Dockerfile.worker` | `[cloud,generate]` | Cloud Run **job** — runs once per request, to completion, then dies |

Both read the **same** `DATABASE_URL` secret — the managed Postgres is the whole of their shared
state; they never call each other in-process. The API is granted `run.invoker` **on that one job**,
nothing else. `deploy_cloudrun_neon.sh` does all of it (`JOB=forge-pdm-generate` by default).

The free-tier envelope is enforced at the door, not by hoping: a request over the caps (30 vehicles ·
14 days · **200 unit-days**) is a **400** naming the bound, and stored rows are retained to a budget
(200k rows ≈ 86 MB of Neon's free 0.5 GB) with the oldest runs evicted — a public generate endpoint
with no retention is a database that fills up and takes the prediction log down with it. Without
`GENERATION_JOB`/`GENERATION_PROJECT`/`GENERATION_REGION`, the API reports honestly that fleet
generation is unavailable (503) rather than quietly generating in-process; the rest of the demo is
unaffected (so HF Spaces and a local `pdm serve` are untouched).

> **Local development.** There is no Cloud Run on a laptop, so `GENERATION_LOCAL_WORKER=1` makes the
> API spawn the same `pdm generate-run` entry point as a detached **subprocess**. Be precise about
> what that is: the API process still never runs the forge (a test asserts it), but one host with two
> processes is **not** two deployable units — do not read a local demo as evidence for the deployed
> topology.
>
> ```bash
> export DATABASE_URL="sqlite:///$PWD/gen.db"   # any SQLAlchemy URL; Neon in the cloud
> export GENERATION_LOCAL_WORKER=1
> pdm serve                                     # → /demo now offers "Generate your own fleet"
> ```

### Reference deploy — Cloud Run + Neon (free), defined in Terraform (F17)

**The infrastructure is `terraform/`. That is the source of truth.** The deploy script no
longer creates anything — it builds the two images (Terraform does not build images) and then
calls `terraform apply`. A `gcloud … create` anywhere outside Terraform is a bug: it produces
resources the state file does not know about.

Five managed resources plus their identities: **Artifact Registry** · the **Cloud Run service**
(API) · the **Cloud Run job** (the F14a generation worker) · **Secret Manager** (the Neon URL) ·
**IAM** — and two dedicated least-privilege service accounts, one per deployable unit.

#### What Terraform does NOT manage, and why

- **Neon.** It is not a GCP resource. It stays a **documented manual prerequisite**: create a
  free Neon project, take the connection string, hand it in as a variable. A community provider
  exists but wants a Neon API key — a *new* secret class to protect — and the DB password would
  land in state either way, so it buys no hygiene. (ADR-027.)
- **Container images.** Cloud Build builds them; Terraform consumes their URIs as inputs.

#### One-time setup

1. Create a **Neon** project (neon.tech) → copy the connection string.
2. Authenticate. **Both** of these — `gcloud auth login` alone does not give Terraform
   credentials; it needs Application Default Credentials:

```bash
gcloud auth login
gcloud auth application-default login      # ← Terraform reads ADC, not the gcloud login
gcloud config set project "$PROJECT_ID"
```

3. **Bootstrap the state bucket.** It cannot create itself — the config that would create it is
   the config that needs somewhere to keep its state. One command, once, on purpose:

```bash
gcloud storage buckets create "gs://${PROJECT_ID}-tfstate" \
  --location=us-central1 \
  --default-storage-class=STANDARD \
  --uniform-bucket-level-access \
  --public-access-prevention
gcloud storage buckets update "gs://${PROJECT_ID}-tfstate" --versioning
```

   **Keep it regional.** GCP's Always Free tier covers 5 GB of *regional* standard storage in
   `us-central1`/`us-east1`/`us-west1`; multi-region `US` is **not** free-tier eligible. This
   project's state is ~32 KB against that 5 GB, so the backend genuinely costs $0 — but only in
   the regional configuration above.

   Versioning is not decoration: state is Terraform's *belief* about what exists, and a
   truncated or corrupted write is recoverable only if the previous version survives.

#### The state file

**It never goes in git** (`.gitignore` enforces it), and it is **secrets-adjacent**: the Neon
connection string is written into it *in plaintext*. `sensitive = true` redacts a value from CLI
output; it does **not** encrypt it in state. That is a property of Terraform, not a defect here —
and it is the whole reason the backend is a private, public-access-prevented bucket rather than a
file next to the code.

#### Deploy

```bash
PROJECT_ID=forge-pdm-mlops \
TF_VAR_neon_database_url='postgresql+psycopg://user:pass@ep-xxx.<region>.aws.neon.tech/neondb?sslmode=require' \
bash scripts/deploy_cloudrun_neon.sh
```

Note the `postgresql+psycopg://` dialect — Neon hands out `postgresql://`, but the `[cloud]`
extra installs psycopg **3**. Pass the secret via `TF_VAR_` (the environment), never a
`terraform.tfvars` file: `*.tfvars` is gitignored precisely because it is how a live connection
string gets committed by accident.

On a **first** deploy into a fresh project, run `terraform -chdir=terraform apply` once before
the script — the Artifact Registry repo must exist before an image can be pushed to it.
Chicken-and-egg, stated rather than papered over.

#### Review a change before it happens

This is what the imperative script could never give you:

```bash
cd terraform
terraform plan          # exactly what will change, before anything changes
```

#### Tear-down

Deliberately a **two-step**. `deletion_protection` defaults to **on**, so `terraform destroy`
refuses out of the box — the Cloud Run service is the live demo linked from the README, and it
should not be one mistyped command from deletion. Removing the guard is its own reviewable act:

```bash
cd terraform
terraform apply   -var deletion_protection=false     # 1. remove the guard (and commit it to state)
terraform destroy -var deletion_protection=false     # 2. now it will go
```

Then delete the Neon project by hand (Terraform never owned it) and, if you want the last trace
gone, the state bucket.

#### ⚠ Terraform does not know whether your application works

It knows the **resources exist and match the config**. It will print `Apply complete! 0 errors`
over a Cloud Run service that 500s on every request — and during F17 it did exactly that, twice:
once over a job whose IAM role was missing a permission the config had wrong, and once over a job
sitting in a failed `Ready` state that the spec did not capture. Both were caught by an
end-to-end request, not by Terraform. **`plan` is not a test.** Verify the app separately:

```bash
curl -s "$URL/health"                                  # the app answers
curl -s -X POST "$URL/demo/generate" -H 'Content-Type: application/json' \
     -d '{"n_units":8,"days":7,"seed":42}'             # the web→worker→Neon chain works
```

When it finishes it prints the service URL; verify the runtime **and** the managed resource:

```bash
curl -s "$URL/health"     # first hit cold-starts + bakes the demo (~1–2 min)
# {"status":"ok","model_loaded":true,"model_version":"1"}

curl -s -X POST "$URL/demo/predict" -H 'Content-Type: application/json' \
  -d '{"readings":[{"engine_speed_rpm":1850,"coolant_temp_c":112,"oil_pressure_kpa":180,"engine_load_pct":88,"fuel_rate_lph":42,"boost_pressure_kpa":210,"egt_c":620,"def_level_pct":45,"vibration_mms":7.8}]}'
# ...,"persisted":true   ← the managed Postgres write path (persisted:false = the driver/URL is missing)
# then open  $URL/demo  in a browser — the row appears in the recent-predictions panel (read-back).
```

Put the `$URL/demo` link in the README (the F7 DoD), alongside the F6 `/health` badge.

**Tear down** (stop all billing; delete the Neon project separately in its console):

```bash
gcloud run services delete forge-pdm-mlops --region "$REGION"
```

### Alternative — Cloud Run + Cloud SQL (paid)

`scripts/deploy_cloudrun.sh` is the same flow with a GCP-native Cloud SQL instance instead of
Neon (adds `gcloud services enable sqladmin.googleapis.com` to the one-time step). Tear down
its instance with `gcloud sql instances delete forge-pdm-pg` on top of the service delete.

## Alternatives (same image, different host)

- **Render** — a free Docker web service; set the Dockerfile path to `Dockerfile.hf` and the
  port to 8000. Cold-starts after 15 min idle (first `/health` after sleep is slow but works).
- **Fly.io** — `fly launch --dockerfile Dockerfile.hf`; scale-to-zero free allowance (needs a
  card on file). Most "real infra" flavour.

The HF, Cloud Run, Render, and Fly.io targets all serve the same self-contained image; only
the platform glue differs. Cloud Run + a managed Postgres (Neon or Cloud SQL) is the one that
also closes the **managed-cloud** gate (a managed runtime + a managed resource), which the
others — free hosting — do not.
