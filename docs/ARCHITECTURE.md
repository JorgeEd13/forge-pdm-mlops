# Architecture — forge-pdm-mlops

The durable design. Volatile status lives in [`STATE.md`](STATE.md); the rationale
behind each choice in [`DECISIONS.md`](DECISIONS.md).

## One paragraph

A **canonical dataset config** (`configs/dataset.json`) + a **pinned generator**
(`can-telemetry-forge`) reproduce the **full** telemetry dataset identically on any
machine. A **data layer** turns its `readings` table into a leakage-safe modelling
frame; a **training layer** fits two cheap models and logs both to **MLflow**,
registering the winner; a **registry layer** governs metric-gated promotion of a
`production` **alias** + rollback; a **serving layer** (**FastAPI**) exposes exactly that
aliased production model over HTTP; a **monitoring layer**
(**Evidently**) measures drift when the generator's `season` shifts the
distribution; and a **Prefect flow** ties detect→retrain→evaluate→promote into one
DAG, scheduled on free cloud runners by **GitHub Actions**.

## Data flow

```
configs/dataset.json + pinned can-telemetry-forge
        │  (full regeneration — same on every machine, ADR-001)
        ▼
   readings table ──► data.py ──► features.py ──► (X_train/X_test, y) split BY UNIT
        │                                              │
   [offline? fall back to                              ▼
    data/sample_readings.parquet                  train.py ──► MLflow runs
    — smoke fixture only]                              │   (LogReg + LightGBM)
                                                       ▼
                                                 pick winner ──► registry.py
                                                                   │  (gated promote)
                                                                   ▼
                                                        Model Registry (production)
                                                                   │
                                          ┌────────────────────────┴───────────┐
                                          ▼                                     ▼
                                     serve.py (FastAPI)                   monitor.py (Evidently)
                                     /predict /health /model-info        drift report + decision
                                                                                 │
                                                                                 ▼
                              flows.py (Prefect):  detect_drift ─[drift?]─► retrain ─► evaluate ─► promote
                                          ▲
                              .github/workflows/retrain.yml  (cron, free cloud runners)
```

## Modules (target)

| Module | Responsibility |
|--------|----------------|
| `config.py` | Paths, MLflow wiring, seeds, thresholds — one source of truth. |
| `data.py` | Regenerate the full dataset from the canonical config (fixture fallback offline). |
| `features.py` | Era-NULL handling, **leakage guard**, **unit-grouped** train/test split; optional `signal_suspect` feature. |
| `detect.py` | F2.5 detection ladder (multivariate + temporal + autoencoder), signals-only suspicion per row. |
| `detect_score.py` | Grade the ladder vs. ground-truth labels (the **only** place labels are read); tie-aware alarm budget. |
| `suspect.py` | Combine the ladder → leakage-safe `signal_suspect` feature + a data-quality (forensic) watcher. |
| `models.py` | LogReg baseline + LightGBM contender behind one `fit`/`predict_proba`; validated tuned `overrides`. |
| `tune.py` | F2.6 **grouped-CV Optuna** HPO per model on the cleaned frame; tracked to MLflow; tuned params feed `train`. |
| `diagnostics.py` | F2.6 model diagnostics (importance/calibration/threshold/learning-curve artifacts) + training watchers (overfit-gap, majority-baseline). |
| `train.py` | Train both, log to MLflow, return the winner by the primary metric; optional tuned/cleaned/`--audit`/`--diagnose`. |
| `registry.py` | **Metric-gated** promotion to a `production` **alias** (MLflow 3, not deprecated stages) + tag-recorded **rollback**. |
| `serve.py` | FastAPI serving the `production`-**aliased** model (`/predict` → failure probabilities, `/health`, `/model-info`); lazy cached load via the native flavor; follows a promotion/rollback with no redeploy. |
| `monitor.py` | Evidently drift report (baseline vs. season shift) + a drift decision. |
| `flows.py` | Prefect flow: detect_drift → [branch] → retrain → evaluate → promote. |
| `upload.py` | F8 bring-your-own-data: parse + **fuzzy column mapping** → a scorable frame (unmapped signal → era-NULL). |
| `store_pg.py` | F7 prediction log on the managed Postgres; **graceful degrade to `None`** when no `DATABASE_URL`. |
| `generate.py` | F14a: the generation **spec + caps** (the free-tier envelope) and the **per-vehicle roll-up** rule. Pure — the forge import is lazy, so the API never pays for it. |
| `store_gen.py` | F14a: the runs / generated-readings / roll-up-cache tables — **the only channel between the API and the worker**; plus retention against the free 0.5 GB. |
| `jobs.py` | F14a: **the web/worker boundary itself.** Starts a Cloud Run **Job** execution (stdlib `urllib` + the metadata token). The reason `BackgroundTasks` is not here is ADR-026 / S2. |
| `worker.py` | F14a: the **second deployable unit's** entry point (`pdm generate-run`, `Dockerfile.worker`) — the *only* process that runs the forge. |
| `cli.py` | `pdm` entry point dispatching to the above. |

### The two deployable units (F14a)

The system is no longer one container, and that is load-bearing — it is what F17 (Terraform) and F16
(Kubernetes) describe.

```
  browser ──POST /demo/generate──► serve.py (Cloud Run SERVICE)
                                      │  writes a `queued` run           ┌───────────────────┐
                                      ├─────────────────────────────────►│  Neon Postgres    │
                                      │  jobs.py: start a job execution  │  (shared state —  │
                                      ▼                                  │   the ONLY channel│
                            worker.py (Cloud Run JOB) ──generates──────► │   between them)   │
                            Dockerfile.worker, [cloud,generate]          └───────────────────┘
                            runs once → stores rows → exits                        │
                                                                                   │
  browser ──GET  …/report──────► serve.py: score the stored rows with the ◄────────┘
                                 model promoted RIGHT NOW (cached per version,
                                 so a promotion/rollback re-scores — ADR-008/009)
```

The API **never** runs the forge (asserted by test); the worker **never** scores. Both properties are
deliberate: the first is what makes the topology real, the second is what keeps the registry's
"promote and the answer changes, with no redeploy" property true on the report too.

## Cross-cutting invariants

- **Determinism.** One seed threads data → split → train. Same seed → same metrics
  (mirrors the generator's hard invariant). No bare global randomness.
- **One dataset across machines.** Only `configs/dataset.json` + the pinned
  generator define the data; never per-machine forks (ADR-001).
- **The fixture is not a training set.** `data/sample_readings.parquet` is a reduced
  offline smoke fixture; reported models train on the full regenerated dataset only.
- **Local-first, free.** MLflow on a local file backend; Prefect in-process for
  tests/local; CI never installs the generator or hits the network.
- **Honest framing.** The drift→retrain loop is a demonstrated closed loop on
  synthetic data, not a live-production claim.
