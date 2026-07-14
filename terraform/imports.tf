// Adopting the LIVE infrastructure (F17 / ADR-027).
//
// The infra already exists — F7 and F14a created it imperatively. So the first job is not
// to build anything, it is to make Terraform's model agree with reality. Two ways to do
// that, and only one is sane:
//
//   - RECREATE from scratch: destroy and re-apply. Rejected. It would delete the Artifact
//     Registry (taking both images with it, forcing a rebuild), drop the live demo, and buy
//     nothing — the published URL is the deterministic project-number form, so it is not
//     even what is being protected. You do not tear down a working system to prove you
//     could have built it.
//   - IMPORT: describe what exists, adopt it, and iterate the HCL until `plan` reports no
//     changes. A clean plan against the live deploy IS the F17 definition of done — it is
//     the proof that the config is a faithful description of reality rather than a
//     plausible-looking file that has never been checked against anything.
//
// These are `import` BLOCKS, not `terraform import` CLI invocations. The CLI version
// mutates state as a side effect and leaves no record; a block is committed, appears in
// `plan` before it does anything, and documents how each resource was adopted.
//
// They are idempotent to keep: once a resource is in state, its import block is a no-op.
// Left in place deliberately, as the record of the F7-imperative → F17-declarative
// migration. They can be deleted once that history stops being interesting.

// --- APIs (already enabled by the old script's manual prerequisite step) ---------------

import {
  to = google_project_service.required["run.googleapis.com"]
  id = "forge-pdm-mlops/run.googleapis.com"
}

import {
  to = google_project_service.required["artifactregistry.googleapis.com"]
  id = "forge-pdm-mlops/artifactregistry.googleapis.com"
}

import {
  to = google_project_service.required["secretmanager.googleapis.com"]
  id = "forge-pdm-mlops/secretmanager.googleapis.com"
}

import {
  to = google_project_service.required["cloudbuild.googleapis.com"]
  id = "forge-pdm-mlops/cloudbuild.googleapis.com"
}

// --- Artifact Registry -----------------------------------------------------------------

import {
  to = google_artifact_registry_repository.images
  id = "projects/forge-pdm-mlops/locations/us-central1/repositories/forge-pdm"
}

// --- Secret Manager --------------------------------------------------------------------
//
// Version 4 is `latest`. Versions 1-3 are the residue of re-running the imperative deploy
// script, which appended a new version every time it ran — visible churn that nothing was
// tracking. Terraform adopts v4 and will only add another if the URL actually changes.

import {
  to = google_secret_manager_secret.db_url
  id = "projects/forge-pdm-mlops/secrets/forge-pdm-db-url"
}

import {
  to = google_secret_manager_secret_version.db_url
  id = "projects/forge-pdm-mlops/secrets/forge-pdm-db-url/versions/4"
}

// --- Cloud Run: the service and the job -------------------------------------------------

import {
  to = google_cloud_run_v2_service.api
  id = "projects/forge-pdm-mlops/locations/us-central1/services/forge-pdm-mlops"
}

import {
  to = google_cloud_run_v2_job.generate
  id = "projects/forge-pdm-mlops/locations/us-central1/jobs/forge-pdm-generate"
}

// --- IAM ---------------------------------------------------------------------------------
// IAM member import ids are space-separated: "<resource> <role> <member>".

// NOTE — the two bindings the imperative script made against the DEFAULT COMPUTE SA
// (secretAccessor on the secret, run.invoker on the job) were imported in the first F17
// apply and then deliberately REMOVED in the second, when the runtime moved to dedicated
// least-privilege service accounts. They are not imported here any more because they no
// longer exist. Their import ids, for the record, were:
//
//   projects/forge-pdm-mlops/secrets/forge-pdm-db-url roles/secretmanager.secretAccessor serviceAccount:958199756179-compute@developer.gserviceaccount.com
//   projects/forge-pdm-mlops/locations/us-central1/jobs/forge-pdm-generate roles/run.invoker serviceAccount:958199756179-compute@developer.gserviceaccount.com
//
// The `api_invokes_job` binding still exists, but against the NEW api service account, and
// Terraform created it — so there is nothing to adopt.

import {
  to = google_cloud_run_v2_service_iam_member.public
  id = "projects/forge-pdm-mlops/locations/us-central1/services/forge-pdm-mlops roles/run.invoker allUsers"
}
