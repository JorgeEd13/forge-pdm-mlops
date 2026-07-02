#!/usr/bin/env bash
# Deploy the serving image to Google Cloud Run + Cloud SQL (F7 — managed cloud).
#
# This is the gate-closer: the SAME self-contained image the HF Space runs (Dockerfile.hf,
# which bakes a fixture-trained DEMO registry at startup and is already $PORT-aware via
# scripts/hf_entrypoint.sh) is built, pushed to Artifact Registry, and run on **Cloud Run**
# — a *managed* container runtime, not a VM — with a **managed Cloud SQL for Postgres**
# instance behind it (the demo's prediction log, DATABASE_URL via Secret Manager). Managed
# runtime + managed resource = "operate managed cloud in production", which "a container
# that builds" is explicitly NOT.
#
# Nothing secret is committed: the DB password is generated at run time and stored in
# Secret Manager; this script only references it by name.
#
# Prereqs (one-time, interactive — do these on the machine with your gcloud auth):
#   gcloud auth login
#   gcloud config set project "$PROJECT_ID"
#   gcloud services enable run.googleapis.com sqladmin.googleapis.com \
#       artifactregistry.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com
#
# Usage (from the repo root):
#   PROJECT_ID=my-proj REGION=us-central1 bash scripts/deploy_cloudrun.sh
#
# Idempotent-ish: re-running reuses an existing SQL instance / repo / secret and just
# rolls a new revision. Deleting everything afterwards is one `gcloud run services delete`
# + `gcloud sql instances delete` (see docs/DEPLOY.md, "Tear down").
set -euo pipefail

# --- config (override via env) ------------------------------------------------
PROJECT_ID="${PROJECT_ID:?set PROJECT_ID to your GCP project}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-forge-pdm-mlops}"
REPO="${REPO:-forge-pdm}"                       # Artifact Registry repo
SQL_INSTANCE="${SQL_INSTANCE:-forge-pdm-pg}"    # Cloud SQL for Postgres instance
SQL_TIER="${SQL_TIER:-db-f1-micro}"             # smallest/cheapest shared-core tier
DB_NAME="${DB_NAME:-forge_pdm}"
DB_USER="${DB_USER:-forge_app}"
SECRET_NAME="${SECRET_NAME:-forge-pdm-db-url}"  # Secret Manager: the DATABASE_URL
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}:latest"

echo "[deploy] project=${PROJECT_ID} region=${REGION} service=${SERVICE}"

# --- 1. Artifact Registry repo (create if missing) ----------------------------
if ! gcloud artifacts repositories describe "${REPO}" --location "${REGION}" >/dev/null 2>&1; then
  echo "[deploy] creating Artifact Registry repo ${REPO}…"
  gcloud artifacts repositories create "${REPO}" \
    --repository-format=docker --location "${REGION}" \
    --description "forge-pdm-mlops serving images"
fi

# --- 2. Build & push the image (Cloud Build — no local Docker daemon needed) ---
# Uses the self-contained bake image; the demo registry bakes at container startup.
echo "[deploy] building ${IMAGE} via Cloud Build (Dockerfile.hf)…"
gcloud builds submit --tag "${IMAGE}" --config /dev/stdin <<'YAML'
steps:
  - name: gcr.io/cloud-builders/docker
    args: ["build", "-f", "Dockerfile.hf", "-t", "${_IMAGE}", "."]
images: ["${_IMAGE}"]
YAML
# (If the inline config is awkward in your gcloud version, replace the block above with:
#   gcloud builds submit --tag "${IMAGE}"  — after renaming Dockerfile.hf to Dockerfile,
#   or add a cloudbuild.yaml. The intent: build Dockerfile.hf and push to ${IMAGE}.)

# --- 3. Cloud SQL for Postgres instance (create if missing) -------------------
if ! gcloud sql instances describe "${SQL_INSTANCE}" >/dev/null 2>&1; then
  echo "[deploy] creating Cloud SQL Postgres instance ${SQL_INSTANCE} (${SQL_TIER})…"
  gcloud sql instances create "${SQL_INSTANCE}" \
    --database-version=POSTGRES_15 --tier "${SQL_TIER}" --region "${REGION}"
fi
gcloud sql databases create "${DB_NAME}" --instance "${SQL_INSTANCE}" 2>/dev/null || true

# A generated password, stored ONLY in Secret Manager (never printed, never committed).
DB_PASS="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
gcloud sql users create "${DB_USER}" --instance "${SQL_INSTANCE}" --password "${DB_PASS}" 2>/dev/null \
  || gcloud sql users set-password "${DB_USER}" --instance "${SQL_INSTANCE}" --password "${DB_PASS}"

CONN_NAME="$(gcloud sql instances describe "${SQL_INSTANCE}" --format='value(connectionName)')"

# --- 4. DATABASE_URL → Secret Manager -----------------------------------------
# Cloud Run reaches Cloud SQL over a unix socket at /cloudsql/<connectionName>; psycopg
# takes that as the `host` query param. SQLAlchemy URL:
DATABASE_URL="postgresql+psycopg://${DB_USER}:${DB_PASS}@/${DB_NAME}?host=/cloudsql/${CONN_NAME}"
if ! gcloud secrets describe "${SECRET_NAME}" >/dev/null 2>&1; then
  gcloud secrets create "${SECRET_NAME}" --replication-policy=automatic
fi
printf '%s' "${DATABASE_URL}" | gcloud secrets versions add "${SECRET_NAME}" --data-file=-

# --- 5. Deploy the Cloud Run service ------------------------------------------
# --add-cloudsql-instances wires the socket; DATABASE_URL comes from the secret; Cloud Run
# injects $PORT (the entrypoint already honours it). Public (unauthenticated) so the demo
# link is clickable; the endpoint only serves a demo model.
echo "[deploy] deploying Cloud Run service ${SERVICE}…"
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated \
  --add-cloudsql-instances "${CONN_NAME}" \
  --set-secrets "DATABASE_URL=${SECRET_NAME}:latest" \
  --cpu 1 --memory 1Gi --timeout 300 --min-instances 0

URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --format='value(status.url)')"
echo "[deploy] done."
echo "[deploy] live:      ${URL}/health"
echo "[deploy] demo page: ${URL}/demo"
echo "[deploy] verify:    curl -s ${URL}/health   # (first hit cold-starts + bakes the demo — allow ~1–2 min)"
