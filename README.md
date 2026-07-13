<p align="center">
  <img src="assets/logo.png" alt="forge-pdm-mlops" width="440">
</p>

<h1 align="center">forge-pdm-mlops</h1>

<p align="center"><em>A predictive-maintenance ML pipeline with the production spine closed end to end — train → registry → serve → drift → retrain → cloud.</em></p>

<p align="center">
  <a href="https://forge-pdm-mlops-958199756179.us-central1.run.app/demo"><img src="https://img.shields.io/badge/▶%20try%20it%20live-interactive%20demo-4285F4?logo=googlecloud&logoColor=white" alt="Try it live"></a>
  <img src="https://img.shields.io/badge/ROC--AUC-~0.82-success" alt="ROC-AUC ~0.82">
  <img src="https://img.shields.io/badge/tests-169%20offline-success" alt="169 offline tests">
  <img src="https://img.shields.io/badge/data-100%25%20synthetic-blueviolet" alt="100% synthetic data">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License"></a>
</p>

---

## ▶ Try it — no signup, no key

**[Interactive demo](https://forge-pdm-mlops-958199756179.us-central1.run.app/demo)** — score machine telemetry for failure risk, or upload your own CSV/Parquet.
Runs on **Google Cloud Run** with a **managed Neon Postgres** behind it, at **$0**.

Also live on Hugging Face Spaces: [`/health`](https://jorgeed-forge-pdm-mlops.hf.space/health) · [`/model-info`](https://jorgeed-forge-pdm-mlops.hf.space/model-info) · [`/docs`](https://jorgeed-forge-pdm-mlops.hf.space/docs)

> **The honesty boundary.** The data is **100% synthetic**. The served model is a fixture-trained **demo**, labelled as such by `/model-info` — the ≈0.82 figure is the full-data model trained locally, and it is the only number ever reported. Nothing here claims a live *production* deployment; the drift→retrain loop is a demonstrated closed loop on synthetic data.

---

## Quickstart

```bash
pip install -e .[dev]
pytest -q                 # 169 offline tests — no network, no generator needed
pdm --version
```

That runs against a committed **smoke fixture**. To train on real (full, regenerated) data:

```bash
pip install -e .[generate]     # the pinned generator
pdm train                      # train both models → MLflow → register the winner
pdm serve                      # FastAPI over the production-aliased model
pdm flow --season heatwave     # the marquee: detect drift → retrain → promote-or-hold
```

| | |
|---|---|
| `pdm train` | train both models, track to MLflow, register the winner |
| `pdm promote` / `pdm rollback` | metric-gated promotion to `production`; deterministic rollback |
| `pdm serve` | FastAPI `/predict`, `/health`, `/model-info` |
| `pdm monitor` | Evidently drift report + the share-of-features decision |
| `pdm flow` | the closed `detect → retrain → promote-or-hold` loop |
| `pdm detect` / `pdm tune` / `pdm sequence` / `pdm ceiling` | the modelling arc (see below) |

---

## What this is

The **MLOps half of a two-repo story.** Its companion
[`can-telemetry-forge`](https://github.com/JorgeEd13/can-telemetry-forge) is a clean-room
generator of synthetic, **SAE J1939-grounded** heavy-equipment telemetry. This repo is the
**ML system in production on top of it**.

> *Built the data engine, then the ML-in-production system over it.*

Nothing about the model is clever — **that's the point.** The dataset is diverse,
statistically credible and fully reproducible, so the **pipeline around it** is the thing
on display.

---

## The four things worth your time

### 🚦 A worse model cannot reach production

Promotion to the `production` alias is **metric-gated**: `pdm promote` reads the candidate's
and the incumbent's ROC-AUC from their MLflow source runs and moves the alias **only if the
candidate clears the gate**. A worse candidate does *not* promote — asserted by test — and
that rejection is a **governed, structured outcome, not an exception**. Rollback restores the
prior version deterministically.

The load-bearing part: **the auto-retrain loop routes through that same gate, unchanged.** A
retrained model that doesn't beat the incumbent is **held, not shipped** (proven by a test that
sets an impossible bar). So *auto-retrain* can never quietly mean *auto-degrade* — the guarantee
isn't hoping the retrain is good, it's the gate for when it isn't.

### 🐛 The bug was in the data, not the model

The classifier scored **≈ 0.55 — chance.** Rather than tune it, I measured *why*: a failing
unit's pre-failure rows were statistically **identical** to its healthy rows. There was no
signal to learn.

The root cause was upstream, in the **generator** — failures had a *when*, but the sensors had
no *path toward* it. I fixed it there (progressive pre-failure degradation, no label leak), and
the same model reached **≈ 0.82 ROC-AUC**. Two attempts to also rebalance the failure hazard
were measured and **rejected**.

*Finding that my own showcase was measuring at chance — and saying so — is the point.*

### 🧭 Then I measured that 0.82 is the *data's* ceiling

Instead of chasing a better model forever, I characterized the ceiling from three independent
angles: an **AUC decomposition** by time-to-failure horizon, a deliberately **label-leaking
upper bound** (a fenced diagnostic, never a reported metric, asserted by test), and an
**out-of-fold stacking redundancy probe**.

All three converged: **≈0.82 is the data's limit, not the pipeline's.** One probe **refuted my
own hypothesis** — stacking beat the best base model by ~+0.007, leaving a little combinable
signal — and I reported that instead of what I expected.

Two more negatives, measured and reported: **grouped-CV Optuna HPO moves the number by +0.003**
(LightGBM) / **+0.000** (LogReg), and a causal **TCN** lands *below* the cheap temporal-feature
LightGBM. Tuning wasn't the lever; the data was. Knowing the ceiling is the licence to **stop
optimizing** and go build the spine.

### 🌩️ Operated on managed cloud — not just containerized

The same image runs on **Google Cloud Run** (a managed serverless container runtime, not a VM)
with a **managed Neon Postgres** behind it. The `/demo` page scores your inputs and **logs each
prediction to the managed database, then reads it back** — so the managed resource has a real
job, not a decorative one. Secrets live in **Secret Manager**; the image builds via **Cloud
Build**. The whole thing runs at **$0** (scale-to-zero + Neon free tier).

`store_pg.open_log()` accepts **any** SQLAlchemy URL, so the same code runs on tmp SQLite in
tests and Postgres in production — and **graceful degradation is a hard invariant**: with no
`DATABASE_URL` the demo simply doesn't persist. Adding a managed resource cannot break any
other deploy.

---

## The drift → retrain loop

The generator exposes a `season` knob that shifts the whole fleet's operating distribution
(a `heatwave` runs the machines hotter). That is the drift stimulus:

```
baseline model  ──serve──►  production
        │
   --season heatwave  ──►  the data distribution drifts
        │
   drift monitor FIRES (Evidently)  ──►  retrain on the new distribution
        │
   re-run the SAME model comparison  ──►  register + promote-or-HOLD (the F3 gate)
```

Drift is decided by a **share-of-features** policy, not a single column — one noisy signal
doesn't trigger a retrain.

```bash
pdm monitor --season heatwave     # the drift report + the decision
pdm flow    --season heatwave     # the full loop, in-process
```

---

## Honest evaluation, by construction

- **A leakage guard that fails the build** if a label-side column reaches the features.
- **Era-gated missingness preserved as signal** — a sensor an older CAN bus never reported is
  `NULL`, not zero; LightGBM consumes the `NaN` natively. No blind imputation.
- **Unit-grouped splits**, so no machine's autocorrelated series straddles the train/test line.
- **One seed** threads data → split → metrics. Same seed, same numbers.
- **Reported models always train on the full regenerated dataset.** The committed
  `data/sample_readings.parquet` is a **smoke fixture** so `clone && pytest` runs offline — it is
  never a training set for a reported number ([ADR-001](docs/DECISIONS.md)).

---

## The stack (and why two orchestration layers)

| Concern | Tool | Note |
|---|---|---|
| Tracking + model registry | **MLflow** | Local SQLite backend — no server, no cost. Built on version **aliases** (the current API; the classic stages are deprecated). |
| Models | **scikit-learn** + **LightGBM** | A LogReg baseline and a LightGBM contender, compared *through MLflow* — model selection as a recorded process, not folklore. |
| Serving | **FastAPI** | Serves the `production`-aliased model; a promotion or rollback changes `/predict` **with no redeploy**. |
| Drift | **Evidently** | Baseline vs. a `season`-shifted distribution, under an auditable share-of-features policy. |
| Orchestration | **Prefect** | Authors `detect → retrain → promote-or-hold`; runs in-process for tests. |
| Scheduled execution | **GitHub Actions** | Triggers the flow on a cron, on free runners. |
| Managed cloud | **Cloud Run** + **Neon** | Managed runtime + managed Postgres, in production, at $0. |

Prefect and Actions sit at **different layers** — Actions is the *scheduler*, Prefect is the
*flow author* — and each closes a distinct gap. Rationale in [ADR-002](docs/DECISIONS.md).

---

## Going deeper

The README is the tour. The substance lives in:

- **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** — module design and data flow.
- **[`docs/DECISIONS.md`](docs/DECISIONS.md)** — the ADRs: why aliases over stages, why a fixture
  is never a training set, why serving loads from a client-resolved path, and the rest.
- **[`docs/ROADMAP.md`](docs/ROADMAP.md)** — every phase (F0–F9) with objective and definition of
  done, including the two deliberately **deferred** ones (RUL reframing, cross-dataset validation
  on NASA C-MAPSS).
- **[`docs/DEPLOY.md`](docs/DEPLOY.md)** — the hosted and managed-cloud deploys.

---

## License

[MIT](LICENSE) © 2026 Jorge Ribeiro
