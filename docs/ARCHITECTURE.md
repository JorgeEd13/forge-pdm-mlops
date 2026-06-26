# Architecture — forge-pdm-mlops

The durable design. Volatile status lives in [`STATE.md`](STATE.md); the rationale
behind each choice in [`DECISIONS.md`](DECISIONS.md).

## One paragraph

A **canonical dataset config** (`configs/dataset.json`) + a **pinned generator**
(`can-telemetry-forge`) reproduce the **full** telemetry dataset identically on any
machine. A **data layer** turns its `readings` table into a leakage-safe modelling
frame; a **training layer** fits two cheap models and logs both to **MLflow**,
registering the winner; a **registry layer** governs stage→production promotion; a
**serving layer** (**FastAPI**) exposes the production model; a **monitoring layer**
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
| `models.py` | LogReg baseline + LightGBM contender behind one `fit`/`predict_proba`. |
| `train.py` | Train both, log to MLflow, return the winner by the primary metric. |
| `registry.py` | Register + **metric-gated** stage→production promotion + rollback. |
| `serve.py` | FastAPI serving the **production** registry model. |
| `monitor.py` | Evidently drift report (baseline vs. season shift) + a drift decision. |
| `flows.py` | Prefect flow: detect_drift → [branch] → retrain → evaluate → promote. |
| `cli.py` | `pdm` entry point dispatching to the above. |

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
