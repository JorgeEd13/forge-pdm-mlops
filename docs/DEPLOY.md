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
curl -s localhost:8000/health      # {"status":"ok","model_loaded":true,"model_version":"1"}
curl -s -X POST localhost:8000/predict \
  -H 'content-type: application/json' \
  -d '{"readings":[{"coolant_temp_c":95,"engine_load_pct":80}]}'
```

The seed step (train + promote on the fixture) has been verified end-to-end natively:
a fresh serving process reading the baked store returns `model_loaded=true`, a real
`/predict` probability, and `/model-info`.

## Alternatives (same image, different host)

- **Render** — a free Docker web service; set the Dockerfile path to `Dockerfile.hf` and the
  port to 8000. Cold-starts after 15 min idle (first `/health` after sleep is slow but works).
- **Fly.io** — `fly launch --dockerfile Dockerfile.hf`; scale-to-zero free allowance (needs a
  card on file). Most "real infra" flavour.

All three serve the same self-contained image; only the platform glue differs.
