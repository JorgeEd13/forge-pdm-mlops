// Terraform + provider pins, and the state backend (F17 / ADR-027).
//
// THE STATE FILE IS NOT SOURCE. It is Terraform's *belief* about what exists — a cache of
// the last observed reality, not a description of the desired one. The desired one is the
// .tf files, and only those are committed. Two consequences, both load-bearing:
//
//   1. It never goes in git. (.gitignore enforces this; see the repo root.)
//   2. It is SECRETS-ADJACENT. `google_secret_manager_secret_version.db_url` carries the
//      Neon connection string, and Terraform records every managed attribute in state —
//      `sensitive = true` redacts it from CLI *output*, it does NOT encrypt it in state.
//      The Neon URL is therefore in this bucket in plaintext.
//
// Hence a private, versioned bucket rather than a file on a laptop:
//   - versioning     → a corrupted or truncated state can be rolled back to a prior version
//   - public access  → prevented at the bucket level (enforced, not merely "not granted")
//   - regional/STD   → us-central1 STANDARD is inside GCP's Always Free 5 GB allowance.
//                      Multi-region `US` is NOT free-tier eligible. If you re-create this
//                      bucket, keep it regional or it starts costing money.
//
// Bootstrap (once, before `terraform init` — the bucket cannot create itself; see
// docs/DEPLOY.md). State for this project is ~50 KB against a 5 GB allowance.

terraform {
  required_version = ">= 1.5" // `import` blocks (config-driven import) land in 1.5.

  backend "gcs" {
    bucket = "forge-pdm-mlops-tfstate"
    prefix = "forge-pdm-mlops"
  }

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.8"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
