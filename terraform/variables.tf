// Inputs. Everything that differs between a project/region lives here, so the same config
// can stand up the system somewhere else — which is the property the imperative script
// never had.

variable "project_id" {
  description = "GCP project id."
  type        = string
  default     = "forge-pdm-mlops"
}

variable "region" {
  description = "Cloud Run + Artifact Registry region. Keep it in the free tier."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run service: the FastAPI serving app."
  type        = string
  default     = "forge-pdm-mlops"
}

variable "job_name" {
  description = "Cloud Run job: the F14a generation worker (a separate deployable unit — ADR-026 S2)."
  type        = string
  default     = "forge-pdm-generate"
}

variable "repo_name" {
  description = "Artifact Registry (docker) repository holding both images."
  type        = string
  default     = "forge-pdm"
}

variable "secret_name" {
  description = "Secret Manager secret holding the Neon DATABASE_URL."
  type        = string
  default     = "forge-pdm-db-url"
}

// --- The images ---------------------------------------------------------------------
//
// TERRAFORM DOES NOT BUILD IMAGES. It codifies infrastructure; Cloud Build builds the
// containers. These are inputs, not outputs: `scripts/deploy_cloudrun_neon.sh` builds and
// pushes both images, then calls `terraform apply`. The default `:latest` matches what is
// live. (`:latest` is a mutable tag, so a re-apply with an unchanged tag is a no-op to
// Terraform even though the image behind it may have changed — an honest limitation of
// tagging this way, and the reason the deploy script forces a new revision itself.)

variable "api_image" {
  description = "Serving image (Dockerfile.hf)."
  type        = string
  default     = null
}

variable "worker_image" {
  description = "Generation-worker image (Dockerfile.worker)."
  type        = string
  default     = null
}

// --- The Neon connection string ------------------------------------------------------
//
// NEON IS NOT A GCP RESOURCE, and Terraform does not manage it (ADR-027). It is a
// documented manual prerequisite: a free-tier Neon project, created once in their web UI.
// Managing it would mean a community provider plus a Neon API key — a *new* secret class
// to protect — and the database password would land in state regardless, so it would not
// even buy secret hygiene.
//
// What Terraform DOES manage is the GCP-side handling of the string: the Secret Manager
// secret, its version, and who may read it. Pass it out-of-band, never in a committed file:
//
//   export TF_VAR_neon_database_url='postgresql+psycopg://…?sslmode=require'
//
// `sensitive` keeps it out of CLI output and logs. It does NOT keep it out of state —
// see the note in versions.tf. That is a property of Terraform, not a bug here, and it is
// why the backend is a private bucket.

variable "neon_database_url" {
  description = "Neon connection string, psycopg3 dialect. Supply via TF_VAR_neon_database_url; never commit."
  type        = string
  sensitive   = true
}

// --- Tear-down ------------------------------------------------------------------------
//
// ON by default, which is why `terraform destroy` does NOT work out of the box — and that
// is deliberate. The Cloud Run service IS the live demo linked from the README and the CV;
// it should not be one mistyped command from deletion.
//
// F17's DoD asks for tear-down to be *documented*, not to be a single trigger. So teardown
// is an intentional two-step: flip this off, apply, then destroy (see docs/DEPLOY.md).
// That is also how this is done on a real team — protect prod, and make removing the
// protection its own reviewable act.

variable "deletion_protection" {
  description = "Cloud Run deletion protection. Set false (and apply) before `terraform destroy`."
  type        = bool
  default     = true
}
