#!/usr/bin/env bash
# Deploy to Cloud Run + Neon.
#
# ⚠ THIS SCRIPT NO LONGER CREATES INFRASTRUCTURE (F17 / ADR-027).
#
# It used to. It was a sequence of `gcloud` commands that created an Artifact Registry repo,
# a Cloud Run service, a Cloud Run job, a Secret Manager secret and two IAM bindings — which
# meant the only description of the infrastructure was *the transcript of a program someone
# ran once*. Nothing could be diffed, reviewed, recreated in another project, or destroyed
# cleanly. That is the defect F17 exists to fix.
#
# The infrastructure is now defined in `terraform/`, and that is the single source of truth.
# This script does the ONE thing Terraform does not do:
#
#     Terraform does not build container images.
#
# So the division of labour is:
#     this script  →  build + push the two images (Cloud Build), then call `terraform apply`
#     terraform/   →  everything else: registry, service, job, secret, IAM, service accounts
#
# If you add a resource, add it to the Terraform — NOT here. A `gcloud ... create` in this
# file is a bug: it recreates the exact defect above, and Terraform will either fight it or,
# worse, quietly not know about it.
#
# ---------------------------------------------------------------------------------------
# Prereqs (one-time, interactive):
#   1. A Neon project + database → its connection string (docs/DEPLOY.md).
#   2. gcloud auth login
#   3. gcloud auth application-default login    ← Terraform needs ADC; `gcloud auth login`
#                                                  alone is NOT enough.
#   4. The Terraform state bucket (it cannot create itself — see docs/DEPLOY.md).
#
# Usage (from the repo root):
#   PROJECT_ID=forge-pdm-mlops \
#   TF_VAR_neon_database_url='postgresql+psycopg://user:pass@ep-xxx.neon.tech/db?sslmode=require' \
#   bash scripts/deploy_cloudrun_neon.sh
#
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID to your GCP project}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-forge-pdm-mlops}"
REPO="${REPO:-forge-pdm}"
JOB="${JOB:-forge-pdm-generate}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="${REPO_ROOT}/terraform"

# The Neon URL is Terraform's input, not ours. Fail early and loudly rather than letting
# `terraform apply` prompt for it halfway through a deploy.
: "${TF_VAR_neon_database_url:?set TF_VAR_neon_database_url (psycopg3 dialect; never commit it)}"

# --- The image tag is the git SHA, not `latest` -----------------------------------------
#
# `:latest` is a MUTABLE tag, and that is a real problem for a declarative deploy: push new
# bytes behind the same tag and Terraform compares "latest" to "latest", sees no change, and
# rolls no new revision. Your code is built, pushed... and not serving. (The old imperative
# script hid this by always calling `gcloud run deploy`, which unconditionally creates a
# revision whether or not anything changed.)
#
# An immutable tag makes the image a real input: the URI changes → Terraform plans a new
# revision → the deploy is visible in the diff, like everything else.
GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse --short HEAD)"
if ! git -C "${REPO_ROOT}" diff --quiet HEAD 2>/dev/null; then
  GIT_SHA="${GIT_SHA}-dirty"
  echo "[deploy] ⚠ working tree is dirty — tagging images ${GIT_SHA}"
  echo "[deploy]   (a '-dirty' image is not reproducible from a commit; fine for iterating,"
  echo "[deploy]    not fine for anything you intend to point at from the README)"
fi

REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}"
API_IMAGE="${REGISTRY}/${SERVICE}:${GIT_SHA}"
WORKER_IMAGE="${REGISTRY}/${JOB}:${GIT_SHA}"

echo "[deploy] project=${PROJECT_ID} region=${REGION} tag=${GIT_SHA}"

# --- 1. Build both images (Cloud Build — no local Docker daemon needed) -------------------
#
# TWO images, because this is a web+worker system (ADR-026 / decision S2), not one container
# with a background thread. Two Dockerfiles, two dependency surfaces, two lifecycles.
#
# NOTE: the Artifact Registry repo must exist before pushing. On a FIRST deploy into a fresh
# project, run `terraform apply` once (it creates the registry) before this build — see
# docs/DEPLOY.md. Chicken-and-egg, stated rather than papered over.

echo "[deploy] building API image (Dockerfile.hf) → ${API_IMAGE}"
gcloud builds submit "${REPO_ROOT}" --project "${PROJECT_ID}" \
  --substitutions "_IMAGE=${API_IMAGE}" --config /dev/stdin <<'YAML'
steps:
  - name: gcr.io/cloud-builders/docker
    args: ["build", "-f", "Dockerfile.hf", "-t", "${_IMAGE}", "."]
images: ["${_IMAGE}"]
YAML

echo "[deploy] building worker image (Dockerfile.worker) → ${WORKER_IMAGE}"
gcloud builds submit "${REPO_ROOT}" --project "${PROJECT_ID}" \
  --substitutions "_IMAGE=${WORKER_IMAGE}" --config /dev/stdin <<'YAML'
steps:
  - name: gcr.io/cloud-builders/docker
    args: ["build", "-f", "Dockerfile.worker", "-t", "${_IMAGE}", "."]
images: ["${_IMAGE}"]
YAML

# --- 2. Apply the infrastructure ----------------------------------------------------------
# Everything else lives in terraform/. The images are inputs to it.

echo "[deploy] terraform apply…"
terraform -chdir="${TF_DIR}" init -input=false
terraform -chdir="${TF_DIR}" apply -input=false \
  -var "project_id=${PROJECT_ID}" \
  -var "region=${REGION}" \
  -var "api_image=${API_IMAGE}" \
  -var "worker_image=${WORKER_IMAGE}"

URL="$(terraform -chdir="${TF_DIR}" output -raw service_url)"

# --- 3. Verify — because Terraform does not know whether the app works ---------------------
#
# `Apply complete! 0 errors` says the RESOURCES match the config. It says nothing about the
# application. Terraform will report success over a Cloud Run service that 500s on every
# request — and during F17 it did exactly that, twice (once on a permission the config had
# wrong, once on a job left in a failed Ready state). The smoke test below is not ceremony.

echo "[deploy] verifying /health (cold start + demo bake — allow ~1-2 min)…"
if curl -fsS --max-time 180 --retry 5 --retry-delay 15 --retry-all-errors "${URL}/health"; then
  echo
  echo "[deploy] done."
  echo "[deploy] demo:   ${URL}/demo"
  echo "[deploy] worker: gcloud run jobs executions list --job ${JOB} --region ${REGION}"
else
  echo
  echo "[deploy] ✗ /health did not come up. The infrastructure applied cleanly and the APP is broken —"
  echo "[deploy]   which is precisely the distinction Terraform cannot make for you."
  echo "[deploy]   Logs: gcloud run services logs read ${SERVICE} --region ${REGION} --limit 50"
  exit 1
fi
