#!/usr/bin/env bash
# Deploy the serving image to Google Cloud Run with **Neon** serverless Postgres as the
# managed resource (F7 — managed cloud, $0 free-tier variant of deploy_cloudrun.sh).
#
# Same gate, zero code change: Cloud Run is the *managed runtime* (free tier, scale to
# zero) and Neon (neon.tech) is the *managed resource* — a serverless Postgres with a real
# free tier, reached over the public internet with TLS instead of the Cloud SQL unix
# socket. store_pg.open_log(url) takes ANY SQLAlchemy URL, so the only difference from the
# Cloud SQL script is *which URL* lands in Secret Manager — no application change.
#
# Why this variant exists: Cloud SQL has no free tier (~$8-10/mo while it exists). Neon's
# free tier closes the "managed cloud resource in production" gate at $0.
#
# Prereqs (one-time, interactive — do these on the machine with your gcloud auth):
#   1. A Neon project + database → copy its connection string (see docs/DEPLOY.md).
#   2. gcloud auth login
#   3. gcloud config set project "$PROJECT_ID"
#   4. gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
#          secretmanager.googleapis.com cloudbuild.googleapis.com
#      (No sqladmin.googleapis.com — there is no Cloud SQL in this variant.)
#
# Usage (from the repo root):
#   PROJECT_ID=my-proj REGION=us-central1 \
#   NEON_DATABASE_URL='postgresql://user:pass@ep-xxx-pooler.us-east-2.aws.neon.tech/dbname?sslmode=require' \
#   bash scripts/deploy_cloudrun_neon.sh
#
# The Neon URL is read from the environment (never committed) and rewritten to the
# psycopg3 dialect + stored in Secret Manager. Re-running rolls a new revision and adds a
# fresh secret version.
set -euo pipefail

# --- config (override via env) ------------------------------------------------
PROJECT_ID="${PROJECT_ID:?set PROJECT_ID to your GCP project}"
NEON_DATABASE_URL="${NEON_DATABASE_URL:?set NEON_DATABASE_URL to your Neon connection string}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-forge-pdm-mlops}"
REPO="${REPO:-forge-pdm}"                        # Artifact Registry repo
SECRET_NAME="${SECRET_NAME:-forge-pdm-db-url}"   # Secret Manager: the DATABASE_URL
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}:latest"

echo "[deploy] project=${PROJECT_ID} region=${REGION} service=${SERVICE} (Neon backend)"

# --- 0. Normalise the Neon URL to the psycopg3 SQLAlchemy dialect -------------
# Neon hands out `postgresql://…`; SQLAlchemy would pick psycopg2 for that scheme, but the
# [cloud] extra installs psycopg (v3), whose dialect is `postgresql+psycopg://`. Rewrite
# only the scheme; keep the ?sslmode=require query (psycopg3 honours it). Idempotent.
DATABASE_URL="${NEON_DATABASE_URL/#postgresql:\/\//postgresql+psycopg://}"
DATABASE_URL="${DATABASE_URL/#postgres:\/\//postgresql+psycopg://}"
case "${DATABASE_URL}" in
  *sslmode=*) : ;;                                  # already has an sslmode
  *\?*)       DATABASE_URL="${DATABASE_URL}&sslmode=require" ;;
  *)          DATABASE_URL="${DATABASE_URL}?sslmode=require" ;;
esac

# --- 1. Artifact Registry repo (create if missing) ----------------------------
if ! gcloud artifacts repositories describe "${REPO}" --location "${REGION}" >/dev/null 2>&1; then
  echo "[deploy] creating Artifact Registry repo ${REPO}…"
  gcloud artifacts repositories create "${REPO}" \
    --repository-format=docker --location "${REGION}" \
    --description "forge-pdm-mlops serving images"
fi

# --- 2. Build & push the image (Cloud Build — no local Docker daemon needed) ---
# The self-contained bake image; the demo registry bakes at container startup.
echo "[deploy] building ${IMAGE} via Cloud Build (Dockerfile.hf)…"
gcloud builds submit --substitutions "_IMAGE=${IMAGE}" --config /dev/stdin <<'YAML'
steps:
  - name: gcr.io/cloud-builders/docker
    args: ["build", "-f", "Dockerfile.hf", "-t", "${_IMAGE}", "."]
images: ["${_IMAGE}"]
YAML

# --- 3. DATABASE_URL → Secret Manager -----------------------------------------
if ! gcloud secrets describe "${SECRET_NAME}" >/dev/null 2>&1; then
  gcloud secrets create "${SECRET_NAME}" --replication-policy=automatic
fi
printf '%s' "${DATABASE_URL}" | gcloud secrets versions add "${SECRET_NAME}" --data-file=-

# Cloud Run's runtime service account needs to read the secret.
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
gcloud secrets add-iam-policy-binding "${SECRET_NAME}" \
  --member "serviceAccount:${RUNTIME_SA}" \
  --role roles/secretmanager.secretAccessor >/dev/null

# --- 4. Deploy the Cloud Run service ------------------------------------------
# No --add-cloudsql-instances: Neon is reached over the public internet (egress is free).
# DATABASE_URL is injected from the secret; Cloud Run injects $PORT (entrypoint honours it).
# Public (unauthenticated) so the demo link is clickable; it only serves a demo model.
echo "[deploy] deploying Cloud Run service ${SERVICE}…"
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated \
  --set-secrets "DATABASE_URL=${SECRET_NAME}:latest" \
  --cpu 1 --memory 1Gi --timeout 300 --min-instances 0

URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --format='value(status.url)')"
echo "[deploy] done."
echo "[deploy] live:      ${URL}/health"
echo "[deploy] demo page: ${URL}/demo"
echo "[deploy] verify:    curl -s ${URL}/health   # (first hit cold-starts + bakes the demo — allow ~1-2 min)"
