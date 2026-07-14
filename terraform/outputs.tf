output "service_url" {
  description = "The live demo. Cloud Run also serves the deterministic project-number form, which is what the README links."
  value       = google_cloud_run_v2_service.api.uri
}

output "job_name" {
  description = "The generation worker (F14a). Executions: gcloud run jobs executions list --job <name> --region <region>"
  value       = google_cloud_run_v2_job.generate.name
}

output "registry" {
  description = "Artifact Registry path the deploy script pushes both images to."
  value       = local.registry
}

output "runtime_service_accounts" {
  description = "One dedicated least-privilege identity per deployable unit (ADR-027). The API may start the job; the worker may not."
  value = {
    api    = google_service_account.api.email
    worker = google_service_account.worker.email
  }
}
