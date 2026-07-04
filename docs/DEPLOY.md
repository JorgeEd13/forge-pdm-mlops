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

### Reference deploy — Cloud Run + Neon (free)

**One-time setup:**

1. Create a **Neon** project (neon.tech) and copy its connection string, e.g.
   `postgresql://user:pass@ep-xxx-pooler.<region>.aws.neon.tech/neondb?sslmode=require`.
2. Authenticate + enable the APIs (no `sqladmin` — there is no Cloud SQL here):

```bash
gcloud auth login
gcloud config set project "$PROJECT_ID"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
    secretmanager.googleapis.com cloudbuild.googleapis.com
```

**Deploy** (the Neon URL is read from the environment, rewritten to the `postgresql+psycopg://`
dialect, and stored in Secret Manager):

```bash
PROJECT_ID=my-proj REGION=us-central1 \
NEON_DATABASE_URL='postgresql://user:pass@ep-xxx-pooler.<region>.aws.neon.tech/neondb?sslmode=require' \
bash scripts/deploy_cloudrun_neon.sh
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
