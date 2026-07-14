# Epoch 2 — from one risk score to a per-vehicle, multi-fault maintenance report

**Status:** in progress — **F14a is DONE** (2026-07-14, ADR-026); **F17 is next**. Resequenced
2026-07-14 — see **S1** (F14 splits; its topology half leads) and **S2** (the generation worker is a
separate deployable unit; it **shipped that way**). Execution order is
**~~F14a~~ → F17 → F16 → F10 → F11 → F12 → F13 → F14b → F15**; phase IDs are stable, only the order
moved. **Owner doc:** this file is the single source of truth for the Epoch-2 body of work (phases
**F10–F17**). `docs/STATE.md` points here; per-phase ADRs get written into the relevant repo's
`DECISIONS.md` at implementation time (forge-pdm from **ADR-020**, can-telemetry-forge from
**ADR-021**) — but the *decision + rationale* are pre-captured here so nothing is lost between
sessions.

> **How to use this document.** Read the **Vision**, then **§1 Locked decisions** (the
> irrecoverable reasoning — read this before touching any phase), then the phase you're on.
> Every phase carries a **⚠ DO NOT MISREAD** block calling out the specific way a future
> session could drift and build the wrong thing. If you find yourself about to contradict a
> locked decision, stop and re-read its rationale — it was chosen against a named alternative
> for a named reason.

---

## Vision (what we are actually building)

Today `/demo` takes a single row of nine J1939 signals and returns one blended risk
percentage. Epoch 2 turns that into: **the model learns how each vehicle behaves over time,
and produces a per-vehicle report naming which subsystem(s) need maintenance soon, with a
plain-language "why."**

Target output, per vehicle:

> **Vehicle 4021 — press + engine flagged.** *Hydraulic press:* pressure has sagged ~12% over
> the last 5 days under rising compaction duty — likely seal wear, schedule inspection. *Engine:*
> coolant temperature is running abnormally high for this unit's own history and its region's
> climate — overheat risk within ~48 h.

Four capability shifts get us there:

1. **Multi-label, not single-event.** A vehicle can be building toward *several* failures at
   once; the model predicts each subsystem independently.
2. **Per-vehicle + per-region baselines.** "Abnormal" is relative to *this unit's* own history
   and *its* region's climate, not a fleet-wide constant.
3. **A narrative report.** Per-prediction attribution → templated human-readable reasons. The
   features that drive the prediction *are* the sentences.
4. **Optional operator enrichment.** Non-CAN signals the operator can supply (e.g. compaction
   duty) that sharpen specific modes when present and degrade gracefully when absent.

Plus the productization the demo needs: **generate-your-own-data**, a **TP/FP/FN feedback
loop**, and the two infra gates (**K8s**, **IaC**) that the resulting multi-service system
legitimately earns.

**Recall bias:** throughout, prefer catching a real failure over avoiding a false alarm —
maintenance PdM is safer erring toward inspection. Tune thresholds **per mode** for high recall.

---

## §1 — Locked decisions (the irrecoverable reasoning — read before any phase)

Each decision records **what**, **why**, the **alternative we rejected**, the **trap it
avoids**, and **where the ADR lands**. Do not silently reverse one; if a phase seems to need
it, the rationale here is the thing to argue against first.

### D1 — Reconcile with F2.7: the sequence model is *re-opened*, not resurrected against a settled finding
- **What.** F2.7/F2.8 measured, on the **current** task (single binary `failure_within_h`,
  earliest-event, no per-unit baseline, no enrichment), that *temporal helps a little and deep
  (the TCN, ADR-007) doesn't earn its place* over tuned LightGBM (~0.82 ceiling, tuning bought
  +0.003). **That finding stands — for that task.**
- **Why re-open.** Epoch 2 **changes the task**: multi-label, per-vehicle/region temporal
  baselines, and optional enrichment. A conclusion measured on the old task does **not**
  transfer to the new one. The sequence contender competes again **because the problem is
  different**, judged by the promotion gate per mode — not by assumption in either direction.
- **Alternative rejected.** (a) "We already proved deep loses, skip it" — wrong, that was a
  different task. (b) "The task is harder so deep must win now, migrate to it" — equally
  unproven; the tabular reality (heterogeneous, differently-weighted signals) still favors
  trees on the non-temporal part.
- **Trap it avoids.** A future session reading only F2.7 kills the contender and never measures
  it on the task where it might actually win (the trajectory-heavy modes). Or the reverse:
  someone rips out LightGBM for a net without measuring.
- **Note.** This may also activate **F2.9** (RUL / graded-label reframing), which was deferred
  *by design* — the ramp the binary label flattens is exactly what a per-vehicle report wants.
- **ADR:** forge-pdm ADR-020 (re-open under new task).

### D2 — Committee of per-mode specialists (one winner per mode), NOT one multi-output model
- **What.** For **each** failure mode, the MLflow promotion gate picks the single best
  architecture (LightGBM *or* the TCN sequence net). They run side by side, each owning its
  mode. Reuse the existing "two models, one interface, gate picks winner" pattern (models.py /
  train.py / registry.py, ADR-008).
- **Why.** (a) Per-mode recall thresholds need independent control. (b) Modes have different
  signatures and plausibly different best architectures (bearing 0.873 is high-vibration and
  tree-friendly; overheat 0.772 is trajectory-heavy and may favor the sequence net). (c)
  "Different architectures won different modes, the gate picked each" is the senior MLOps story
  and the whole point of a governed registry.
- **Alternative rejected.** A single multi-output model (shared trees / one net with N heads):
  loses per-mode threshold control and per-mode architecture selection.
- **Trap it avoids.** Collapsing back to one model "for simplicity" and losing the per-mode
  tuning that the high-recall requirement depends on.
- **ADR:** forge-pdm ADR-021.

### D3 — Ensembling *within* a mode is opt-in, not baseline
- **What.** Blending GBM + TCN for the *same* mode is allowed only where a mode is stubborn
  (likely overheat), as a deliberate tuning move.
- **Why.** Within-mode ensembling can add recall but doubles that mode's serving cost and
  complicates attribution (the "why" report). Not worth it until a mode demands it.
- **Trap it avoids.** Ensembling everything by default → a heavy, hard-to-explain serving path
  that busts the $0 CPU envelope (see G1) for marginal gain.

### D4 — Multi-label = independent per-mode labels; drop "earliest-wins" in the forge
- **What.** can-telemetry-forge currently samples the **earliest** failure across modes → one
  event per unit (`failure_within_h` + `failure_mode`, ADR-009/ADR-020 there). Change it so each
  mode has an **independent** label (`failure_within_h_<mode>`) and a unit can build toward
  several at once. The per-mode progressive degradation (forge ADR-020) already supports
  overlapping signatures.
- **Why.** "One **or multiple** parts" is impossible with earliest-wins.
- **Leakage subtlety (critical).** In forge-pdm, `failure_mode` is excluded from features as
  label-side leakage (features.py, ADR-003) — *that stays true*. The new per-mode **targets**
  (`failure_within_h_<mode>`) are legitimate *targets*, not features. The leakage guard must be
  extended to exclude **all** per-mode target columns from every mode's feature matrix, not just
  the one being predicted.
- **Alternative rejected.** Keep earliest-wins and predict only the "dominant" fault — throws
  away the multi-fault capability that is the whole point.
- **Trap it avoids.** (a) A future session re-reads ADR-009 ("single event, single source of
  truth") and thinks multi-label violates it — it doesn't; it *generalizes* the label derivation,
  still in one place. (b) Leaking one mode's target into another mode's features.
- **ADR:** can-telemetry-forge ADR-021 (independent labels); forge-pdm ADR-022 (leakage guard
  extension).

### D5 — Per-vehicle/region baselines are where the accuracy lives, and they *are* the report
- **What.** Engineer features relative to the unit's **own** history and its **region's**
  climate: rolling means/slopes, delta-vs-own-baseline, region-normalized z-scores,
  time-since-last-anomaly. Built on the existing detection ladder (detect.py) + `signal_suspect`
  (suspect.py).
- **Why.** The temporal, per-unit signal ("abnormal *for this unit*, declining over 5 days") is
  exactly what a single-row model can't see and what the report must say. These features are
  **SHAP-attributable**, so each one becomes a sentence: "vibration slope +X over 5 days."
- **Trap it avoids.** Reaching for a big sequence model to capture temporality that is cheaper,
  more explainable, and served-CPU-friendly as engineered features. The features come first;
  the sequence net competes *on top of* them (D1/D2).
- **ADR:** forge-pdm ADR-023 (temporal/per-unit feature set + narrative report).

### D6 — Optional operator-enrichment features: leading/causal only, degrade gracefully
- **What.** A fourth feature class: non-CAN signals an operator can supply that sharpen specific
  modes (**exemplar: compaction duty — cycles/discharged tonnage — driving hydraulic-press
  wear**). In the forge, generate a per-unit duty signal and make the press-mode hazard depend on
  **accumulated duty** (same mechanism as the existing `wear` gain); expose it as a **maskable**
  column so "operator didn't report it" is simulated.
- **Why.** Reframes the product from "here's my data" to "the more you tell it, the better it
  predicts *your* fleet." The effect is **real in the synthetic ground truth** (duty genuinely
  drives the hazard), so the demo isn't theater.
- **Two hard constraints (non-negotiable):**
  1. **Leakage / leading-indicator discipline.** Enrichment features are **duty / load /
     context** — causes that *build toward* failure, available *before* the maintenance need.
     Never a concurrent symptom of the failure. Every one runs through `assert_no_leakage`.
  2. **Graceful degradation.** The model must not *depend* on an enrichment feature. Defenses,
     use both: (a) LightGBM native NaN handling (learns a default direction — a real reason to
     keep GBM in the committee for these features); (b) **feature-dropout augmentation** — blank
     the enrichment feature on a random fraction of training rows so the model is robust to its
     absence.
- **Alternative rejected.** Making enrichment features mandatory inputs — breaks for the many
  operators who don't have them and kills the CAN-only baseline.
- **Trap it avoids.** An enrichment feature that *looks* brilliant offline (because it leaks the
  outcome) and is useless live; or a model that silently gets worse for operators who don't
  enrich.
- **Scope discipline.** Catalogue enrichment in the taxonomy now; **implement ONE exemplar**
  (compaction duty → press) end-to-end before generalizing. One clean proof > five half-wired
  channels.
- **ADR:** can-telemetry-forge ADR-022 (duty signal + duty-driven press hazard + maskable
  column); forge-pdm ADR-024 (enrichment feature class + dropout-augmentation).

### D7 — Compute: train on the notebook GPU, serve on Cloud Run CPU
- **What.** All Epoch-2 training runs on the notebook (RTX 4050); serving stays Cloud Run **CPU**
  at $0. The i3 desktop is out of the loop (see career memory `resources_compute`).
- **Why.** The sequence contender needs the GPU to train comfortably; the i3 couldn't. Standard
  train-GPU / serve-CPU split.
- **Determinism caveat (must record, don't regress silently).** GPU training is **not**
  bitwise-deterministic by default. The CLAUDE.md "one seed → same metrics" claim is protected
  by: keeping the **data** path exactly reproducible (unchanged), pinning torch deterministic
  flags for the neural path where feasible, and **explicitly** stating any residual metric jitter
  for the neural contender rather than pretending byte-identical. The LightGBM path stays exactly
  reproducible.
- **ADR:** forge-pdm ADR-025 (GPU-train/CPU-serve + determinism boundary).

### G1 — The $0 envelope is the binding constraint on the interactive demo
- **What.** On-demand generation + browsable dataset + multi-label inference must all fit Cloud
  Run free CPU (~512 MiB, request timeout) and Neon free (~0.5 GB).
- **Consequences (not optional).** (a) Generation is a **bounded demo fleet / short window**;
  (b) generation is **async** (kick off → poll → view), never synchronous in a request; (c) the
  stored dataset is a **capped sample**; (d) whatever model wins **must serve on CPU** — this
  caps the sequence net's size (a small TCN can; a large transformer can't). The cap is a
  feature: it keeps the neural contender honest-sized.
- **Trap it avoids.** A synchronous full-fleet generation that OOMs/timeouts the free container,
  or a Neon dataset that blows the free tier.

### H1 — Honesty boundaries (extend the existing `demo=fixture` discipline)
- The feedback loop demonstrates **human-in-the-loop mechanics on synthetic data** — it does
  **not** "learn from real users in production." Keep an explicit banner, as with `demo=fixture`
  (ADR-014) and the ≈0.82 framing.
- The enrichment "it improved recall" claim must be backed by a **real** effect in the synthetic
  ground truth (D6), never a staged correlation.

### S1 — F14 splits; its topology half comes FIRST, ahead of F10–F13 *(decided 2026-07-14)*
- **What.** F14 is cut in two. **F14a (topology)** — co-deploy the forge as its own deployable
  unit, bounded async generation, capped store, browse — ships **before** F10–F13. **F14b
  (report)** — the multi-label per-vehicle narrative over the generated data — ships after F12.
  Execution order becomes: **F14a → F17 → F16 → F10 → F11 → F12 → F13 → F14b → F15.**
- **Why.** F16/F17 are blocked on F14's **multi-service topology**, not on its report. The topology
  half depends on nothing in F10–F13: the forge exists (0.2.0), the API is live on Cloud Run, the
  Neon store is live. Under the original order the two open gates — the stated reason Epoch 2 has a
  back half — sat five sessions behind four phases of *modelling* work that closes neither of them.
  F14a scored by today's single-label model, rolled up per vehicle, is already an honest product
  increment ("generate a fleet → see which vehicles are at risk"); F10–F13 then upgrade **what the
  report says**, in place, without moving a service boundary.
- **This does not contradict §3's "product-first" preamble — it supersedes it.** That preamble was
  a sequencing *map*, never a rationale-backed lock; no D-numbered decision in this section requires
  the product phases to precede F14. The §1 decisions that *do* constrain F14 — **G1** (bounded +
  async + capped) and **H1** (honesty banner) — are satisfiable today, unchanged.
- **Cost, accepted with eyes open.** The report renderer gets paid for twice (single-label in F14a,
  multi-label in F14b), and the stored-dataset schema may be paid for once when F10 flips the forge
  to independent per-mode labels. Both are confined to the **presentation/scoring** layer; the
  topology layer (job kickoff, worker, polling, caps, service split) is untouched by F10–F13.
- **Alternative rejected.** Keep product-first and land F14 complete-in-one-pass (zero rework) —
  rejected because it buys tidiness with ~5 sessions of gate latency on the one axis that is
  actually blocking JD access.
- **Trap it avoids.** A future session reads §3's "Sequencing is product-first" line, concludes the
  reorder was drift, and reverts to F10-first. It was not drift. It was chosen against the
  zero-rework alternative, for the reason above.
- **⚠ What F14a must NOT become.** A thin excuse. It still owes a *real* topology (see S2) and a
  *real* user-visible capability (generate → browse → per-vehicle risk roll-up). If it degenerates
  into "a second container that does nothing", F16 loses its justification and S1 was a mistake.
- **ADR:** forge-pdm ADR-026 (written at F14a implementation time, together with S2).

### S2 — Generation is a SEPARATE DEPLOYABLE UNIT, not a FastAPI `BackgroundTask` *(decided 2026-07-14; resolves the §5 open question)*
- **What.** F14a's async generation runs as its **own deployable unit** — a **Cloud Run Job** (or a
  second Cloud Run service) invoked by the API — writing to the shared store. Not
  `BackgroundTasks`, not a thread in the API process.
- **Why — this single choice is what makes F16 and F17 honest.** A `BackgroundTask` leaves the
  system as **one container**. K8s over one container is precisely the resume-driven development
  that a reviewer can smell, and Terraform would have one service to codify. A real web+worker
  split gives: a genuine second lifecycle/scaling unit → **Deployment + Job** on `kind` (F16), and
  a second real resource for Terraform to codify (F17). It also **costs nothing** — Cloud Run Jobs
  are free-tier eligible, and a scale-to-zero worker is *cheaper* than an always-on one.
- **Why it's the right engineering call anyway, gates aside.** G1 already forbids synchronous
  in-request generation (free-tier CPU/RAM/timeout). Once generation is async and bounded, an
  out-of-process unit is the *normal* answer — the API stays responsive and a runaway generation
  can't wedge the serving container.
- **Trap it avoids.** Reaching for `BackgroundTasks` in F14a because it is three lines of code, and
  thereby silently destroying the justification for both remaining gates. If you find yourself
  writing `background_tasks.add_task(...)`, stop and re-read this decision.
- **ADR:** forge-pdm ADR-026 (with S1).

---

## §2 — The failure taxonomy (four buckets)

The step-1 deliverable of F10 is this table, filled in and grounded in the forge's **actual
signal spec** (each candidate tied to a real observable channel + a feasible signature + a
leakage-safety note). Structure:

1. **Already generated** — `overheat` (coolant/EGT), `oil_starve` (oil pressure under load),
   `bearing` (vibration + wear). Live today.
2. **Generatable + CAN-observable (candidates to add)** — each needs a signature signal the forge
   can emit + a degradation map + a hazard. Candidates to evaluate: hydraulic-press pressure loss,
   fuel/injector degradation, aftertreatment/DPF clogging, transmission/EGR. **Prune to a small
   set; don't add all.**
3. **Optional operator-enrichment (non-CAN, leading/causal, degrade-gracefully)** — exemplar:
   compaction duty → press wear (D6). Catalogue others; implement the exemplar only.
4. **Real but not observable at all → explicitly out of scope** — stating what a CAN bus *can't*
   see is a strength, not a gap.

> **⚠ Framing note.** Buckets 1–2 keep the "predictable from CAN" story; bucket 3 upgrades it to
> "CAN is the always-available baseline; operator enrichment sharpens it." Both are honest; don't
> flatten them into "predict everything."

---

## §3 — Phases (F10–F17)

**Execution order (per S1, 2026-07-14):**

> **F14a → F17 → F16 → F10 → F11 → F12 → F13 → F14b → F15**

Phase **IDs are stable** (F16 = K8s, F17 = IaC) — they are cited from the career system and from
the pre-registered deep-dives; only the *order* moved. The topology-bearing half of F14 comes
first because that — and not the report — is what F16/F17 are blocked on (**S1**); the generation
worker is a separate deployable unit because that is what makes both gates honest (**S2**); and
**F17 runs before F16** because Terraform is justified *by the project* (F7 shipped imperative — a
real, nameable defect) while K8s is justified *by the market*, and enumerating the real resources
in Terraform is the direct input to the K8s manifests (the dependency is one-way). One phase per
session.

*(The superseded "product-first" ordering — F10–F13, then F14, then the gates — is retained in
S1's rationale, along with why it was reversed. Do not silently restore it.)*

### F10 — Failure taxonomy + forge multi-mode independence
- **Objective.** Enumerate the taxonomy (§2) and flip the forge from earliest-wins to
  **independent per-mode labels**; add the pruned bucket-2 signatures.
- **How.** Fill the §2 table against the signal spec; extend `labels/failure.py` to emit
  `failure_within_h_<mode>` per mode; add new signature signals + degradation maps + hazards for
  the chosen bucket-2 modes; update the offline fixture to carry all modes independently.
- **Key decisions.** D4 (independent labels), D1 (task re-opens).
- **DoD.** Forge emits independent per-mode labels; a unit can carry multiple; new modes have a
  signature + progressive degradation; deterministic; forge tests green; taxonomy table committed.
- **⚠ DO NOT MISREAD.** This *generalizes* the single-source-of-truth label derivation (forge
  ADR-009/020), it does not violate it — still derived in exactly one place. Do not keep
  earliest-wins "to be safe."

### F11 — Multi-label committee: per-mode model selection + high-recall thresholds
- **Objective.** One model per mode; LightGBM baseline vs the TCN contender (ADR-007) competing
  through the promotion gate; recall-first thresholds per mode.
- **How.** Extend features.py leakage guard for all per-mode targets (D4); train per mode; reuse
  `ceiling.py` `by_mode`/`by_horizon` for per-mode AUC + TTF sharpening; set per-mode thresholds
  for high recall; the gate promotes a winner **per mode**.
- **Key decisions.** D2 (committee), D3 (no default within-mode ensemble), D1, D7 (train on GPU).
- **DoD.** A registered winner **per mode** (mix of architectures allowed); per-mode held-out AUC
  + recall reported; a worse candidate does not promote (asserted); split stays **by unit**.
- **⚠ DO NOT MISREAD.** The sequence net is evaluated because the **task changed** (D1) — not
  because F2.7 was wrong. Do not skip it citing F2.7; do not delete LightGBM citing "harder task."

### F12 — Per-vehicle/region temporal features + narrative report
- **Objective.** The per-unit/region baseline features and the human-readable "why" report.
- **How.** Add rolling/slope/delta-vs-own-baseline/region-z-score/time-since-anomaly features on
  the detection ladder; per-prediction SHAP attribution → templated report sentences; per-vehicle
  roll-up ("press + engine flagged").
- **Key decisions.** D5 (features are the report), D2.
- **DoD.** A per-vehicle report renders naming flagged subsystem(s) with attributed reasons; the
  reasons trace to specific features; offline test on the fixture.
- **⚠ DO NOT MISREAD.** Temporality goes into **features first** (cheap, explainable, CPU-served),
  not into "switch to a big sequence model." The net competes on top of these, per the gate.

### F13 — Optional operator-enrichment (exemplar: compaction duty → press)
- **Objective.** Prove the enrichment pattern end-to-end with one exemplar.
- **How.** Forge: per-unit duty signal + duty-driven press hazard + maskable column. Model:
  enrichment feature class with **feature-dropout augmentation** + LightGBM native-NaN; measure
  recall lift **when present** vs graceful behavior **when absent**.
- **Key decisions.** D6 (leading/causal only, degrade gracefully), H1 (real effect, not staged).
- **DoD.** With duty present, press-mode recall measurably improves; with it masked, the model
  degrades gracefully (no worse than CAN-only baseline); leakage guard passes on the enrichment
  feature; the lift is a real effect in the synthetic ground truth.
- **⚠ DO NOT MISREAD.** Implement **one** exemplar, not the whole bucket-3 catalogue. Enrichment
  features are duty/context (leading), never symptoms of the failure.

### F14a — Generate-your-own-data: the topology — **DONE (2026-07-14, ADR-026)**
- **Objective.** Co-deploy the forge so a user generates a bounded synthetic dataset, stores it,
  browses it, and gets a **per-vehicle risk roll-up** — scored by **today's** single-label demo
  model. This is the phase that **creates the multi-service topology** F16/F17 depend on.
- **How.** The API exposes *kick off → poll → view*. Generation runs in a **separate deployable
  unit** (Cloud Run Job or a second service — **S2, non-negotiable**), which runs the forge and
  writes a **capped sample** to the store (Neon / object store). Paginated browse. Score the
  generated rows through the existing `_score_frame` core (F8, ADR-017) and roll the row-level
  probabilities up **per vehicle** (max / high-risk-share over the unit's rows). Extends F8 —
  that's *upload yours*, this is *generate one*.
- **Key decisions.** **S1** (this half comes first), **S2** (separate unit, not `BackgroundTask`),
  **G1** ($0 envelope ⇒ bounded + async + capped), **H1** (honesty).
- **DoD.** A user kicks off a generation, polls it to completion, browses the stored sample, and
  sees a per-vehicle risk roll-up — all inside the free Cloud Run + Neon envelope. The generation
  worker is a **genuinely separate deployable unit** (asserted: the API process does not run the
  forge). The `demo=fixture` honesty banner holds on the roll-up. Caps enforced and fail loud
  (fleet size, window, stored rows) — 4xx, never a wedged container. Offline tests.
- **⚠ DO NOT MISREAD.** (a) Generation is bounded + async **out-of-process** — not sync-in-request
  (G1), and not a `BackgroundTask` (S2). (b) The report here is deliberately the **existing
  single-label risk score**, rolled up per vehicle — the multi-label narrative is **F14b**, after
  F12. Do not pull the report forward; do not let F14a become "a second container that does
  nothing" (S1's ⚠ block).
- **ADR:** forge-pdm ADR-026 (topology + async-as-separate-unit + the S1 sequencing rationale).

### F14b — Generate-your-own-data: the multi-label report *(after F12)*
- **Objective.** Upgrade F14a's per-vehicle roll-up **in place** into the full multi-label
  narrative report (the Vision's target output: *"Vehicle 4021 — press + engine flagged…"*).
- **How.** Swap the single-score roll-up for the committee (F11) + the SHAP-attributed narrative
  (F12) over the generated dataset. **No service boundary moves** — this is a
  presentation/scoring-layer change on the topology F14a already shipped.
- **Key decisions.** D2 (committee), D5 (the features are the report), H1.
- **DoD.** The generated dataset yields a per-vehicle report naming the flagged subsystem(s) with
  attributed reasons; the F14a caps, async mechanism, and honesty banner are untouched.
- **⚠ DO NOT MISREAD.** This is the **rework S1 knowingly paid for**. It is not a redo of F14a.

### F15 — HITL feedback loop (TP / FP / FN)
- **Objective.** Let users mark predictions TP/FP or report a missed FN; fold feedback into a
  retrain trigger.
- **How.** Capture feedback into the store (store_pg.py / Neon); a Prefect flow (flows.py) folds
  accumulated feedback into a retrain that routes through the **same** promotion gate (ADR-013).
- **Key decisions.** H1 (demonstrated HITL on synthetic, not production learning).
- **DoD.** Feedback is captured and a retrain trigger consumes it through the gate; explicit
  honesty banner that this is demonstrated-on-synthetic.
- **⚠ DO NOT MISREAD.** Do not claim "learns from real users in production." It demonstrates the
  loop mechanics.

### F17 — IaC / Terraform — closes the IaC gate *(runs BEFORE F16, per S1)*
- **Objective.** Codify the managed deploy (Cloud Run API service + the F14a generation job + Neon
  + any bucket + Artifact Registry + Secret Manager + IAM) as Terraform.
- **The honest justification — name the defect.** F7 shipped **imperative**: the live deploy exists
  as `scripts/deploy_cloudrun_neon.sh`, a sequence of `gcloud` commands someone ran **once**. There
  is no source of truth for what the infra *should* be — it cannot be diffed, reviewed, recreated
  in another project/region, or destroyed cleanly. That is a real gap, not an invented one. **This
  is why F17 goes first: it is justified by the PROJECT.** (F16 is justified by the *market* — see
  its ⚠ block. Do not defend the two the same way.)
- **How.** Terraform over the F7 + F14a managed resources; `plan`/`apply`; documented tear-down.
  The deploy script is the spec — read it, don't re-derive the resource list.
- **Why before F16.** Enumerating the real resources in HCL (registry · api service · generation
  job · secret · Neon · bucket · IAM) **is** the input to the K8s manifests. The dependency runs
  one way: Terraform-first makes F16 cheaper; F16-first does nothing for F17.
- **Entry question (resolve at phase start).** **State backend.** The state file is *not* source —
  it is Terraform's **belief** about what exists, it does **not** go in git, and it is
  **secrets-adjacent** (the Neon connection string can land in it in plaintext). Decide: local
  state (gitignored) vs. a GCS free-tier bucket backend. Check the choice against the zero-budget
  constraint before committing to it.
- **DoD.** The managed infra is reproducible from committed Terraform against free-tier resources;
  `plan` on the live deploy is clean (or its drift is explained); tear-down documented; no secret
  in the repo and no state file in git.
- **⚠ DO NOT MISREAD.** **Terraform does not know whether your application works.** It knows the
  *resources exist and match the config* — it will print `Apply complete! 0 errors` over a Cloud
  Run that 500s on every request. Do not claim it validates the app. Requires **F14a** (not the
  full F14) — F14a is what gives it the second real resource to codify.

### F16 — Kubernetes (kind, local, $0) — closes the K8s gate *(runs AFTER F17, per S1)*
- **Objective.** Orchestrate the multi-service system (model API + the generation worker + store)
  under K8s, via a local `kind` cluster at $0.
- **How.** Manifests/Helm for the services (Deployment for the API + Job/Deployment for the
  generation worker; the existing F4 `/health` slots in as the **readiness probe**); local `kind`
  cluster; documented.
- **DoD.** The multi-service system runs on a local `kind` cluster from committed manifests.
- **⚠ DO NOT MISREAD — the honesty this phase lives or dies on.** (a) It is legitimate *because*
  **F14a** made the system genuinely multi-service (**S2**: a real separate deployable unit) — it
  is **not** box-ticking bolted onto a single container. **Requires F14a first.** (b) **This
  project does not need Kubernetes.** Cloud Run + a job remains the right production answer, *after*
  F16 too. F16 is justified **by the market** (a hard requirement in a large slice of MLOps JDs)
  and by the one idea that transfers — declarative desired state + a reconciliation loop — **not**
  by operational need. Say that out loud; it reads as judgment. (c) `kind` is **local**: no cloud
  LB, no real ingress, no node autoscaling. **Never claim "operated a cluster in production."**

---

## §4 — What already exists (don't rebuild)

- **Committee machinery:** models.py / train.py / registry.py — "two models, one interface, gate
  picks winner" + gated promotion + rollback (ADR-008).
- **Sequence contender:** sequence.py — dilated causal TCN (ADR-007), already built and once
  measured (F2.7).
- **Per-mode / horizon metrics:** ceiling.py `by_mode` + `by_horizon` (bearing 0.873 / oil_starve
  0.796 / overheat 0.772; 0.92 within 24 h).
- **Anomaly + suspicion:** detect.py ladder + suspect.py `signal_suspect` + diagnostics.py
  (SHAP-style attribution).
- **Serving + demo:** serve.py FastAPI, bilingual themed `/demo`; upload.py BYO-data (ADR-017).
- **Managed cloud at $0:** Cloud Run + Neon (store_pg.py, ADR-016); Evidently drift (monitor.py);
  Prefect retrain through the gate (flows.py, ADR-013). Live on Cloud Run + HF Space.
- **Forge:** labels/failure.py (3 modes + progressive degradation, forge ADR-020); regions with
  climate; per-unit wear/age/era; anomalies/.

---

## §5 — Open questions to resolve at phase entry (not yet decided)

- ~~**F14a:** the caps; the per-vehicle roll-up rule.~~ **RESOLVED at implementation, 2026-07-14 —
  owner: forge-pdm ADR-026** (do not restate the numbers here; read it there). In brief, and only
  as a pointer: the caps turned out to be a **storage** budget, not a timeout budget (generation is
  ~1 s — it is out-of-process because of **S2**, not because it is slow); and the roll-up is
  **neither** of the two candidates this line offered — both `max-over-rows` and `high-risk-share`
  were measured and both lost to the **peak of a 1-hour rolling mean**, on the demo *and* the
  full-data model. `max` loses *structurally*: it is a one-row statistic and the forge deliberately
  injects outliers, so a single spurious spike flags a healthy vehicle. *(The async-mechanism
  question was already **CLOSED** by **S2** — a separate deployable unit. It shipped that way.)*
- **F17:** the **state backend** — local state (gitignored) vs. a GCS free-tier bucket. The state
  file is secrets-adjacent (Neon connection string) and never goes in git.
- **F16:** Helm vs. plain manifests; whether the generation worker maps to a K8s `Job` or a
  long-lived `Deployment` consuming a queue.
- **F10:** which bucket-2 modes make the cut (prune list), and their exact signature signals.
  **Also:** whether the label flip emits a derived union label (`failure_within_h_any` = OR over
  modes) for backward compatibility with the F14a-era single-score model — cheap, and it keeps the
  live demo scoring while the committee (F11) is still being built.
- **F11:** the per-mode recall threshold targets (how far to bias toward recall).
- **F12:** report template wording + how many top attributions to surface per subsystem.
- **F2.9 activation:** whether the RUL/graded-label reframing is adopted here (D1 note).
