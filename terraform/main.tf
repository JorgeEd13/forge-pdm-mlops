// The managed deploy, as a source of truth (F17 / ADR-027).
//
// This file replaces `scripts/deploy_cloudrun_neon.sh` as the *definition* of the
// infrastructure. The script shipped F7 imperatively — a sequence of `gcloud` commands
// someone ran once — so there was nothing to diff, review, recreate in another
// project/region, or destroy cleanly. F14a made that worse, not better: the deploy grew
// from two resources to five.
//
// ⚠ WHAT THIS DOES NOT DO. Terraform knows that these resources EXIST and MATCH this
// config. It knows nothing whatsoever about whether the application works. It will print
// "Apply complete! 0 errors" over a Cloud Run service that 500s on every request. The
// things that check the app are the test suite, /health, and a real generation run — not
// this file.

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  registry     = "${var.region}-docker.pkg.dev/${var.project_id}/${var.repo_name}"
  api_image    = coalesce(var.api_image, "${local.registry}/${var.service_name}:latest")
  worker_image = coalesce(var.worker_image, "${local.registry}/${var.job_name}:latest")

  // The identity the IMPERATIVE deploy ran both units as: the project's *default compute*
  // service account, used by omission (the script never passed --service-account).
  //
  // ⚠ IT HOLDS roles/editor ON THE PROJECT — a Google default, not something this repo
  // granted. Which means the script's two carefully-scoped bindings (secretAccessor on one
  // secret; run.invoker on one job) were DECORATIVE. An account with Editor can already
  // read every secret and start every job in the project; granting it a narrow role adds
  // nothing. The script had been "tightening" permissions the account already had.
  //
  // WRITING THE IAM DOWN IN HCL IS WHAT MADE THAT VISIBLE. Nobody was hiding it; it simply
  // had no place to be seen. This is the F17 thesis in one finding: the defect was never
  // that the resources were wrong, it was that nothing described them.
  //
  // Kept only to document what we migrated away from. Nothing references it any more.
  legacy_default_sa = "${data.google_project.this.number}-compute@developer.gserviceaccount.com"
}

// --- Runtime identities: one per deployable unit ----------------------------------------
//
// TWO service accounts, not one, because the two units genuinely need different things —
// and "the API may start that job alone" is only a real constraint if the identity holding
// it is an identity that cannot do anything else.
//
//   api    → read the DB secret · start THE generation job · write logs
//   worker → read the DB secret · write logs.   It never starts jobs. It IS the job.
//
// Note what is NOT here: neither needs Artifact Registry read. Cloud Run pulls images with
// its own service agent, not with the runtime identity — a permission it is tempting to
// grant "just in case", which would quietly widen both accounts for no reason.
//
// ⚠ HONEST BOUNDARY: the default compute SA still holds roles/editor. Removing that is out
// of scope here (Cloud Build leans on it, and breaking the build to make an IAM point is a
// bad trade). What changed is that our runtime no longer *runs as* it. The over-privileged
// account still exists; it is simply no longer the one serving traffic.

resource "google_service_account" "api" {
  project      = var.project_id
  account_id   = "forge-pdm-api"
  display_name = "forge-pdm-mlops — serving API runtime"
  description  = "Runs the Cloud Run service. May read the DB secret and start the generation job."
}

resource "google_service_account" "worker" {
  project      = var.project_id
  account_id   = "forge-pdm-worker"
  display_name = "forge-pdm-mlops — generation worker runtime"
  description  = "Runs the Cloud Run job (F14a). May read the DB secret. Cannot start jobs."
}

// Cloud Run needs this to emit logs. Without it the containers run and their logs vanish —
// which is exactly the failure you cannot debug, because the evidence is what went missing.
resource "google_project_iam_member" "log_writer" {
  for_each = {
    api    = google_service_account.api.email
    worker = google_service_account.worker.email
  }

  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${each.value}"
}

// --- APIs --------------------------------------------------------------------------
// Enabling these is part of "reproducible in another project" — the one-time gcloud
// enablement in the old script's header comment was a prerequisite a reader had to notice
// and run by hand. `disable_on_destroy = false`: tearing down this stack must not rip APIs
// out from under anything else in the project.

resource "google_project_service" "required" {
  for_each = toset([
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudbuild.googleapis.com",
  ])

  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

// --- Artifact Registry ---------------------------------------------------------------
// Holds both images: the serving one and the worker one.

resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = var.repo_name
  format        = "DOCKER"
  description   = "forge-pdm-mlops serving + generation-worker images"

  depends_on = [google_project_service.required]
}

// --- Secret Manager: the Neon DATABASE_URL --------------------------------------------
// The secret and its value. The VALUE comes from TF_VAR_neon_database_url and is never
// committed — but it IS written to state (see versions.tf).

resource "google_secret_manager_secret" "db_url" {
  project   = var.project_id
  secret_id = var.secret_name

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "db_url" {
  secret      = google_secret_manager_secret.db_url.id
  secret_data = var.neon_database_url
}

// --- The generation worker: a Cloud Run JOB (F14a / ADR-026 decision S2) ---------------
// Run-to-completion, not a long-lived service: one execution per request, it generates,
// writes to Neon, and dies. This is the second deployable unit — the thing that makes the
// system genuinely web+worker rather than one container with a background thread.

resource "google_cloud_run_v2_job" "generate" {
  project             = var.project_id
  name                = var.job_name
  location            = var.region
  deletion_protection = var.deletion_protection

  template {
    template {
      service_account = google_service_account.worker.email
      max_retries     = 1
      timeout         = "600s"

      containers {
        image = local.worker_image

        resources {
          limits = {
            cpu    = "1"
            memory = "1Gi"
          }

          // ⚠ NO startup_cpu_boost HERE, AND THAT IS NOT AN OVERSIGHT.
          //
          // F14a left a to-do: the worker cold-starts in ~1-2 min, so "give it the
          // --cpu-boost treatment the API already got". Trying it is what killed it —
          // `startup_cpu_boost` is not a valid argument on a Cloud Run *Job*, and
          // `gcloud run jobs` has no `--cpu-boost` flag either. It is a SERVICE-only knob.
          //
          // The reason is the thing worth keeping: boost exists to fix a pathology only
          // services have — CPU throttled to near-zero *outside* request handling, which
          // makes container startup crawl. A job is not request-driven; its task gets full
          // CPU for the whole execution already. There is no throttled state to boost out
          // of, so the flag would be meaningless even if it existed.
          //
          // Which means the ~1-2 min is NOT a CPU problem and no infra flag will fix it.
          // The real cause is dependency weight: mlflow + lightgbm + scikit-learn are BASE
          // dependencies in pyproject.toml, so the worker pulls and imports the entire
          // training stack — none of which it uses, because ADR-026 had it deliberately not
          // score. The fix is a lighter worker image (move the training stack into its own
          // extra), and it is a pyproject change touching every image, so it is its own
          // piece of work — not something to smuggle into an IaC phase.
        }

        env {
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.db_url.secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  // ⚠ THE IAM BINDING MUST BE EXPLICIT — Terraform cannot infer it.
  //
  // Terraform builds its dependency graph from REFERENCES. This job references the worker
  // service account and the secret, so both are correctly ordered before it. It does not
  // reference the *binding between them* — so, left implicit, Terraform is free to deploy
  // the job under its new identity before that identity can read the secret, and Cloud Run
  // validates secret access at deploy time and rejects the revision:
  //
  //   Permission denied on secret … for Revision service account forge-pdm-worker@…
  //
  // This is not a race that "usually works"; the first apply failed on it. An IAM binding
  // that nothing references is invisible to the graph, and invisible dependencies are the
  // main way Terraform configs are wrong in a way `plan` cannot show you — plan renders
  // the resources, not the order.
  depends_on = [
    google_project_service.required,
    google_secret_manager_secret_iam_member.reads_db_url,
  ]
}

// --- The serving API: a Cloud Run SERVICE ----------------------------------------------
// Public, because the whole point is a clickable demo link. It serves a demo model
// (tagged demo=fixture) and holds no user data.

resource "google_cloud_run_v2_service" "api" {
  project             = var.project_id
  name                = var.service_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.deletion_protection

  // Service-level scaling, distinct from `template.scaling` below. Both exist and they are
  // not the same knob: this one is the service's overall scaling mode, the template one is
  // the revision's instance bounds. Declared (rather than left implicit) only so the plan
  // is clean — gcloud sets it, so omitting it would show as perpetual drift.
  scaling {
    min_instance_count = 0
  }

  template {
    service_account                  = google_service_account.api.email
    timeout                          = "300s"
    max_instance_request_concurrency = 80

    scaling {
      min_instance_count = 0 // scale to zero — this is what keeps the demo at $0
      max_instance_count = 20
    }

    containers {
      image = local.api_image

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        cpu_idle          = true // CPU only while serving a request (billed per-request)
        startup_cpu_boost = true // the F7 cold-start fix: full CPU during container start
      }

      // Which job to start. Without these the API honestly reports that fleet generation
      // is unavailable, rather than quietly generating in-process (jobs.open_trigger).
      env {
        name  = "GENERATION_JOB"
        value = google_cloud_run_v2_job.generate.name
      }
      env {
        name  = "GENERATION_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GENERATION_REGION"
        value = var.region
      }

      env {
        name = "DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.db_url.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  // Same invisible-dependency trap as the job above: the service must not be deployed under
  // an identity that cannot yet read the secret it is configured to mount.
  depends_on = [
    google_project_service.required,
    google_secret_manager_secret_iam_member.reads_db_url,
  ]
}

// --- IAM --------------------------------------------------------------------------------
// _member (not _binding, not _policy): additive, and touches ONLY the binding it names.
// A `_policy` resource would take ownership of the resource's whole IAM policy and delete
// anything it does not know about.

// Both units read the DB URL — the API to browse runs and score, the worker to write.
resource "google_secret_manager_secret_iam_member" "reads_db_url" {
  for_each = {
    api    = google_service_account.api.email
    worker = google_service_account.worker.email
  }

  project   = var.project_id
  secret_id = google_secret_manager_secret.db_url.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value}"
}

// The API may start THAT JOB, and only that job — now a real constraint rather than a
// decorative one, because the identity holding it cannot do anything else. Granted to the
// API alone: the worker has no business starting jobs.
//
// ⚠ NOT roles/run.invoker — AND THE IMPERATIVE SCRIPT HAD THIS WRONG.
//
// The script granted `roles/run.invoker` on the job and the system worked, so the binding
// looked correct for months. It was not. `run.invoker` carries `run.jobs.run` but NOT
// `run.jobs.runWithOverrides`, and jobs.py starts the execution WITH per-request env
// overrides (the fleet size, window and seed are passed as overrides — that is the whole
// mechanism). The correct role is the purpose-built one below:
//
//   roles/run.invoker                  → run.jobs.run, run.routes.invoke, run.instances.invoke
//   roles/run.jobsExecutorWithOverrides → run.jobs.run, run.jobs.runWithOverrides,
//                                         run.executions.cancel        ← 3 permissions
//   roles/run.developer                → the same, plus 85 others (create/delete/update
//                                         every job and service). Far too broad.
//
// SO WHY DID GENERATION EVER WORK? Because the runtime was the default compute SA, which
// holds roles/editor — and Editor contains runWithOverrides. The script's binding was not
// what authorised the call; the Editor role it inherited by accident was. The narrow
// binding was not merely decorative, it was INSUFFICIENT, and nothing revealed that while
// an over-privileged account was quietly covering for it.
//
// It surfaced the moment the runtime moved to an account that had *only* what was written
// down — i.e. the moment the permission model was forced to be true. Terraform did not
// catch this (it applied cleanly and reported success); the END-TO-END TEST caught it.
resource "google_cloud_run_v2_job_iam_member" "api_invokes_job" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.generate.name
  role     = "roles/run.jobsExecutorWithOverrides"
  member   = "serviceAccount:${google_service_account.api.email}"
}

// The demo is public.
resource "google_cloud_run_v2_service_iam_member" "public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
