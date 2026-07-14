"""Serving layer — a FastAPI app over the **promoted** model (F4).

F3 governs *which* registered version is in production (the ``production`` alias);
this layer *serves* exactly that version. There is one load-bearing coupling and the
whole phase turns on it: serving resolves the model through
``models:/<name>@production`` — the same alias :mod:`registry` moves — so a promotion
or a rollback in F3 changes what this endpoint answers with **no redeploy, no config
edit**. That is the point of a registry: the alias is the contract between governance
and serving.

Three endpoints:

* ``POST /predict`` — a batch of readings (the J1939 signals, era-NULL allowed as
  JSON ``null``) → the failure probability per row. This is the model's real output:
  the positive-class probability of :data:`config.TARGET`, not a thresholded label.
* ``GET /health`` — liveness + whether a production model is actually loaded. Returns
  200 even with no model yet (the process is up), with ``model_loaded=false`` so an
  orchestrator can tell "process alive" from "ready to serve".
* ``GET /model-info`` — the live production version and the metric it was gated on, so
  a caller can see *which* governed model answered (auditable serving).

**Probabilities, not the pyfunc default.** MLflow's generic ``pyfunc`` predict returns
class *labels* for the sklearn/LightGBM flavors we log — but the product is the failure
*probability*. So the model is loaded through its **native flavor** (which preserves
``predict_proba``) and the positive-class column is returned, exactly the
:meth:`models.Model.predict_proba` contract F2 trains against. The flavor is read from
the registered version's tags so a lightgbm winner and a logreg winner both serve
correctly.

The model is loaded **lazily and cached**: the first request that needs it resolves the
alias and loads the artifact, so the app starts even before anything is promoted (and a
rollback is picked up by clearing the cache). No server, no paid service — the same
local SQLite backend as the rest of the pipeline (ADR-004).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from . import config, features
from . import generate as _generate
from . import jobs as _jobs
from . import registry as _registry
from . import store_gen, store_pg
from . import upload as _upload

# FastAPI/pydantic are the [serve] extra — imported at module load so the app object
# exists for `pdm serve` and the tests, but kept out of the core dependency set.
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


# --- request/response schemas -------------------------------------------------


class PredictRequest(BaseModel):
    """A batch of readings to score.

    ``readings`` is a list of row objects keyed by the :data:`features.FEATURE_COLUMNS`
    signal names. A missing/``null`` value is a *legitimate* input (era-NULL: a sensor
    the unit's era never had) and is passed through to the model, which handles it
    (LightGBM natively, the LogReg pipeline via its imputer) — it is **not** an error.
    """

    readings: list[dict[str, float | None]] = Field(
        ..., min_length=1, description="rows of J1939 signal values (era-NULL as null)"
    )


class PredictResponse(BaseModel):
    """The per-row failure probabilities plus which version produced them."""

    model_version: str
    n_rows: int
    failure_probability: list[float]


class DemoPredictResponse(PredictResponse):
    """A ``/predict`` result plus whether it was persisted to the managed DB (F7).

    ``persisted`` is ``True`` only when a prediction log is configured (managed Postgres —
    Neon behind Cloud Run). On a local run / HF Space it is ``False`` — the prediction still returns;
    it just wasn't logged. The demo page uses it to label the recent-predictions panel
    honestly ("logging off" vs. a live table).
    """

    persisted: bool


class UploadPreview(BaseModel):
    """The map-your-columns step (F8): what we parsed + a proposed column mapping.

    Returned by ``POST /demo/upload`` when **no** mapping is supplied yet. The UI renders a
    dropdown per expected signal, pre-selected with ``suggested_mapping`` (fuzzy auto-match),
    and the tester confirms/corrects before scoring. ``n_signals_matched`` of 9 tells the
    tester up front how much was recognised.
    """

    filename: str
    n_rows: int
    headers: list[str]
    feature_columns: list[str]
    suggested_mapping: dict[str, str | None]
    n_signals_matched: int


class UploadSummary(BaseModel):
    """The batch aggregate shown above the per-row probabilities (F8)."""

    threshold: float
    n_high_risk: int
    pct_high_risk: float
    histogram: list[int]
    bin_edges: list[float]


class UploadScoreResponse(BaseModel):
    """A scored uploaded batch (F8): per-row probabilities + the honest summary.

    ``n_signals_provided`` (of 9) + ``unmapped_signals`` make a *partial* upload honest —
    the missing signals were era-``NULL``, not silently zero. ``demo`` restates that the
    scoring model is the fixture-trained demo (ADR-001), same as ``/model-info``.
    """

    model_version: str
    n_rows: int
    n_signals_provided: int
    mapped_signals: dict[str, str]
    unmapped_signals: list[str]
    failure_probability: list[float]
    summary: UploadSummary
    demo: bool


class GenerateRequest(BaseModel):
    """Ask the forge for a bounded synthetic fleet (F14a).

    The caps live in :mod:`generate` (one owner) and are enforced by
    :meth:`generate.GenerationSpec.validate`, not by Pydantic bounds here — the **worker**
    validates the same spec independently, and a bound duplicated in two schemas is a bound
    that will eventually disagree with itself.
    """

    n_units: int = Field(default=_generate.DEFAULT_UNITS, description="fleet size")
    days: int = Field(default=_generate.DEFAULT_DAYS, description="window length in days")
    seed: int = Field(default=config.DEFAULT_SEED, description="generation seed")


class GenerationRunResponse(BaseModel):
    """A generation run's state — the kick-off reply *and* the polling payload.

    ``worker`` names the topology honestly: ``cloudrun-job`` is the deployed web/worker
    split (decision S2); ``local-subprocess`` is the development stand-in. The API process
    never generates in either case.
    """

    run_id: str
    status: str
    n_units: int
    days: int
    seed: int
    n_rows: int
    expected_rows: int
    error: str | None = None
    worker: str | None = None


class GeneratedRows(BaseModel):
    """A page of the generated fleet (the browse surface)."""

    run_id: str
    total_rows: int
    offset: int
    limit: int
    columns: list[str]
    rows: list[dict[str, object]]


class UnitRiskOut(BaseModel):
    """One vehicle's line in the per-vehicle risk roll-up."""

    unit_id: str
    n_rows: int
    risk: float
    peak: float
    high_risk_share: float
    flagged: bool


class FleetReport(BaseModel):
    """The per-vehicle risk roll-up over a generated fleet (F14a).

    Deliberately the **existing single-label risk score**, rolled up per vehicle — the
    multi-label narrative report ("press + engine flagged, because…") is F14b, after the
    committee (F11) and the attribution features (F12) exist. ``demo`` restates that the
    scoring model is the fixture-trained demo (ADR-001/ADR-014), exactly as ``/model-info``
    and the upload response do.
    """

    run_id: str
    model_version: str
    demo: bool
    n_units: int
    n_flagged: int
    n_rows_scored: int
    flag_threshold: float
    rollup_rule: str
    units: list[UnitRiskOut]


class ModelInfo(BaseModel):
    """The live production model's identity — for auditable serving.

    ``demo`` / ``note`` exist so the endpoint is **self-labelling**: when the served model
    is the fixture-trained demo (the hosted deploy, F6/ADR-014), ``metric_value`` is scored
    on the tiny smoke fixture and is therefore *not* a reported result (it reads high by
    construction — a 20-unit hold-out). The flag + note say so inline, so nobody reads the
    number out of context. On a full-data model these are ``False`` / a plain note.
    """

    registered_model: str
    production_version: str
    primary_metric: str
    metric_value: float
    demo: bool
    note: str


# --- the cached production model ---------------------------------------------


@dataclass
class _LoadedModel:
    """A production model resolved from the registry, cached across requests."""

    version: str
    predict_proba: Any  # callable: DataFrame -> np.ndarray[positive-class proba]


class ModelStore:
    """Lazily resolves and caches the ``production``-aliased model.

    Kept as a small object (not module globals) so a test can point it at a tmp
    registry and so :func:`create_app` can inject one. ``load()`` is idempotent until
    :meth:`clear` is called — which is exactly what a rollback needs (re-resolve the
    alias on the next request).
    """

    def __init__(self, tracking_uri: str | None = None) -> None:
        # Resolve to a concrete URI once (honouring MLFLOW_TRACKING_URI for the
        # container) so the same backend is used for the client, the alias lookup and
        # the artifact load. Tests inject an explicit tmp URI.
        self._tracking_uri = tracking_uri or config.default_tracking_uri()
        self._cached: _LoadedModel | None = None

    def clear(self) -> None:
        """Drop the cached model so the next :meth:`load` re-resolves the alias."""
        self._cached = None

    def _client(self):
        return _registry._client(self._tracking_uri)

    def load(self) -> _LoadedModel:
        """Resolve ``models:/<name>@production`` and cache the loaded estimator.

        Raises :class:`LookupError` if nothing is promoted yet (the alias is unset) —
        the endpoints translate that into a 503, since the process is healthy but has
        no model to serve.
        """
        if self._cached is not None:
            return self._cached

        name = config.REGISTERED_MODEL_NAME
        client = self._client()
        version = _registry.production_version(client, name)
        if version is None:
            raise LookupError(
                f"no production model: the '{_registry.PRODUCTION_ALIAS}' alias on "
                f"'{name}' is unset. Promote a version first (`pdm promote`)."
            )

        proba = _load_predict_proba(client, name, version)
        self._cached = _LoadedModel(version=version, predict_proba=proba)
        return self._cached


def _load_predict_proba(client, name: str, version: str):
    """Load a registered version through its native flavor, return a proba callable.

    **No global MLflow state is touched.** The alias was already resolved to a concrete
    ``version`` through the injected ``client``; here we ask that same client for the
    version's concrete artifact **download URI** and load *that* path, so the flavor
    loaders never fall back to MLflow's process-global tracking/registry URI. (Loading a
    ``models:/<name>@alias`` URI *would* resolve through the global registry — and
    pinning it, even to restore it, leaks into a co-resident ``train``/registry call, as
    a two-model round-trip in one process showed.) Returns a callable
    ``DataFrame -> np.ndarray`` of the **positive-class** probability — the
    :meth:`models.Model.predict_proba` contract used everywhere else.
    """
    import mlflow

    # Resolve the version's artifact URI, then download it to a concrete **local path**.
    # ``download_artifacts`` normalises the (Windows-hostile) ``file:C:/…`` URI MLflow
    # hands back and gives a plain filesystem path the flavor loaders read directly —
    # so no ``models:/`` URI is resolved and no global tracking/registry URI is touched.
    artifact_uri = client.get_model_version_download_uri(name, version)
    local_path = mlflow.artifacts.download_artifacts(artifact_uri=artifact_uri)
    flavor = _model_flavor(local_path)
    if flavor == "lightgbm":
        estimator = mlflow.lightgbm.load_model(local_path)
    else:
        estimator = mlflow.sklearn.load_model(local_path)

    def predict_proba(X: pd.DataFrame) -> np.ndarray:
        proba = np.asarray(estimator.predict_proba(X))
        # positive class (failure_within_h == 1) is column 1 — the models.Model contract.
        return proba[:, 1]

    return predict_proba


def _model_flavor(model_uri: str) -> str:
    """Which MLflow flavor the model at ``model_uri`` was logged with ('lightgbm'|'sklearn').

    Read from the model's own ``MLmodel`` metadata (``get_model_info().flavors``), which
    is populated for every logged model regardless of MLflow version — unlike the
    ``mlflow.log-model.history`` *run* tag, which MLflow 3 no longer writes. The registry
    only ever holds these two flavors (F2 logs lightgbm or the sklearn pipeline); sklearn
    is the safe default if the metadata is somehow ambiguous, and a mismatched load would
    fail loudly rather than mis-serve.
    """
    import mlflow

    flavors = mlflow.models.get_model_info(model_uri).flavors
    if "lightgbm" in flavors:
        return "lightgbm"
    return "sklearn"


# --- the app ------------------------------------------------------------------


def _to_frame(rows: list[dict[str, float | None]]) -> pd.DataFrame:
    """Build the model's input frame from request rows, in the fixed feature order.

    Missing signal keys become NaN (era-NULL, a valid input). The columns are forced to
    the exact :data:`features.FEATURE_COLUMNS` order so the frame matches what the model
    was trained on regardless of JSON key order; :func:`features.assert_no_leakage`
    re-runs as a belt-and-braces guard that no label-side column was smuggled in.
    """
    frame = pd.DataFrame(rows)
    X = frame.reindex(columns=list(features.FEATURE_COLUMNS))
    X = X.apply(pd.to_numeric, errors="coerce")
    features.assert_no_leakage(X)
    return X


def create_app(
    store: ModelStore | None = None,
    prediction_log: store_pg.PredictionLog | None = None,
    generation_store: store_gen.GenerationStore | None = None,
    job_trigger: _jobs.JobTrigger | None = None,
) -> FastAPI:
    """Build the serving app, optionally with an injected :class:`ModelStore`.

    Tests inject a store bound to a tmp registry; ``pdm serve`` uses the default
    (the local SQLite backend). The model is loaded lazily on first use, so the app
    starts cleanly even with nothing promoted yet.

    ``prediction_log`` is the F7 managed-cloud persistence: the demo UI (:func:`/demo`)
    appends each served prediction to it and reads the recent ones back. It defaults to
    :func:`store_pg.open_log`, which returns ``None`` unless ``DATABASE_URL`` is set — so
    a local ``pdm serve``, the HF Space, and CI all run **without** a database and the
    demo simply doesn't persist. Tests inject a log bound to a tmp SQLite file.

    ``generation_store`` + ``job_trigger`` are the F14a generate-your-own-data pair, and
    together they are the web half of the **web/worker split** (ADR-026 / decision S2): the
    store is the shared state, the trigger starts the *other* deployable unit. Both default
    to their env-driven openers and both are ``None`` when unconfigured — in which case the
    generate endpoints answer an honest 503 and every other endpoint is untouched.
    """
    store = store or ModelStore()
    log = prediction_log if prediction_log is not None else store_pg.open_log()
    gen_store = (
        generation_store if generation_store is not None else store_gen.open_store()
    )
    trigger = job_trigger if job_trigger is not None else _jobs.open_trigger()
    app = FastAPI(
        title="forge-pdm-mlops serving",
        description="Serves the production-aliased failure classifier (F4).",
        version=config_version(),
    )
    app.state.store = store
    app.state.prediction_log = log
    app.state.generation_store = gen_store
    app.state.job_trigger = trigger

    @app.get("/")
    def root() -> dict[str, object]:
        """A friendly index so the bare URL isn't a 404 (e.g. the HF Space 'App' tab).

        Not part of the model contract — just points a human at the real endpoints and
        the auto-generated OpenAPI docs.
        """
        return {
            "service": "forge-pdm-mlops serving",
            "description": "FastAPI over the production-aliased failure classifier (F4). "
            "The served model is a fixture-trained DEMO (see /model-info); the reported "
            "~0.82 model is trained on the full dataset locally.",
            "endpoints": {
                "GET /demo": "an interactive 'set parameters → get a prediction' page, "
                "with a 'bring your own data' CSV/Parquet batch upload and a "
                "'generate your own fleet' run",
                "GET /health": "liveness + whether a model is loaded",
                "GET /model-info": "the live production version + the metric it was gated on",
                "POST /predict": "score a batch of J1939 readings → per-row failure probability",
                "POST /demo/generate": "kick off a bounded synthetic fleet (runs in a "
                "separate worker; poll GET /demo/generate/{run_id}, then "
                "/rows to browse it and /report for the per-vehicle risk roll-up)",
                "GET /docs": "interactive OpenAPI docs",
            },
        }

    @app.get("/health")
    def health() -> dict[str, object]:
        """Liveness + readiness. 200 even with no model, so 'up' ≠ 'ready'."""
        try:
            loaded = store.load()
            return {"status": "ok", "model_loaded": True, "model_version": loaded.version}
        except LookupError:
            return {"status": "ok", "model_loaded": False, "model_version": None}

    @app.get("/model-info", response_model=ModelInfo)
    def model_info() -> ModelInfo:
        """The live production version and the metric it was gated on."""
        try:
            loaded = store.load()
        except LookupError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        client = store._client()
        metric = _registry.version_metric(
            client, config.REGISTERED_MODEL_NAME, loaded.version
        )
        # Is this the fixture-trained demo? The seed script (F6) tags the version
        # `demo=fixture`; a full-data model registered by `pdm train` has no such tag.
        version_obj = client.get_model_version(config.REGISTERED_MODEL_NAME, loaded.version)
        is_demo = version_obj.tags.get("demo") == "fixture"
        note = (
            "DEMO model, trained on the committed smoke fixture — this metric is scored on "
            "a tiny hold-out and reads HIGH by construction; it is NOT a reported result "
            "(see ADR-001). The reported ~0.82 ROC-AUC is the full-data model trained locally."
            if is_demo
            else "Full-data model."
        )
        return ModelInfo(
            registered_model=config.REGISTERED_MODEL_NAME,
            production_version=loaded.version,
            primary_metric=config.PRIMARY_METRIC,
            metric_value=metric,
            demo=is_demo,
            note=note,
        )

    def _score_frame(X: pd.DataFrame) -> tuple[str, list[float]]:
        """Resolve the production model and score a prepared frame → (version, probabilities).

        The lowest scoring core, shared by ``/predict``, ``/demo/predict`` and the F8 upload:
        model resolution (503 if nothing is promoted), a belt-and-braces leakage re-check, and
        the positive-class probabilities. Takes an already-built :data:`features.FEATURE_COLUMNS`
        frame so both the JSON path (``_to_frame``) and the upload path
        (:func:`upload.build_frame`) reuse identical scoring.
        """
        try:
            loaded = store.load()
        except LookupError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        features.assert_no_leakage(X)
        proba = loaded.predict_proba(X)
        return loaded.version, [float(p) for p in proba]

    def _score(readings: list[dict[str, float | None]]) -> tuple[str, list[float]]:
        """Score a batch of JSON reading rows → (version, probabilities)."""
        return _score_frame(_to_frame(readings))

    @app.post("/predict", response_model=PredictResponse)
    def predict(request: PredictRequest) -> PredictResponse:
        """Score a batch of readings → per-row failure probability."""
        version, proba = _score(request.readings)
        return PredictResponse(
            model_version=version,
            n_rows=len(proba),
            failure_probability=proba,
        )

    @app.post("/demo/predict", response_model=DemoPredictResponse)
    def demo_predict(request: PredictRequest) -> DemoPredictResponse:
        """Score readings for the demo UI, logging each row to the managed DB (F7).

        The same scoring as ``/predict``, plus: if a prediction log is configured
        (managed Postgres — Neon behind Cloud Run), each scored row is appended to it. When no
        ``DATABASE_URL`` is set the log is ``None`` and this is exactly ``/predict`` with
        a friendlier response shape — the managed resource is a pure enhancement.
        """
        version, proba = _score(request.readings)
        if log is not None:
            for row, p in zip(request.readings, proba):
                log.log(model_version=version, failure_probability=p, readings=row)
        return DemoPredictResponse(
            model_version=version,
            n_rows=len(proba),
            failure_probability=proba,
            persisted=log is not None,
        )

    def _is_demo_version(version: str) -> bool:
        """Is the served version the fixture-trained demo? (the ``demo=fixture`` tag, F6)."""
        try:
            obj = store._client().get_model_version(config.REGISTERED_MODEL_NAME, version)
            return obj.tags.get("demo") == "fixture"
        except Exception:  # never let an audit-label lookup break a prediction
            return False

    @app.post("/demo/upload")
    async def demo_upload(
        file: UploadFile = File(...),
        mapping: str | None = Form(default=None),
    ):
        """Bring-your-own-data (F8): upload a J1939 batch → per-row probabilities + summary.

        **Two modes on one endpoint.** With **no** ``mapping`` it is *preview* mode: parse the
        file (bounded), fuzzy-auto-match its headers onto the nine signals, and return the
        proposed mapping for the tester to confirm (:class:`UploadPreview`). With a confirmed
        ``mapping`` (a JSON ``{signal: header|null}`` string) it is *score* mode: apply the
        mapping (unmapped signals → era-``NULL``), validate it is scorable, and score via the
        shared core (:class:`UploadScoreResponse`).

        **No raw uploaded row is persisted** — an uploaded dataset is arbitrary, so the
        managed-DB posture stays "never store raw uploaded rows" (F7's log is untouched here).
        Every foreseeable bad input (too large, unparseable, not J1939-like, no mapping) is a
        clear 4xx via :class:`upload.UploadError`, never a 500.
        """
        # Read at most the cap (+1 to detect overflow) so a huge upload can't exhaust memory.
        content = await file.read(_upload.MAX_UPLOAD_BYTES + 1)
        try:
            frame = _upload.parse_upload(file.filename or "", content)
            headers = [str(c) for c in frame.columns]

            if mapping is None:
                suggested = _upload.suggest_mapping(headers)
                return UploadPreview(
                    filename=file.filename or "upload",
                    n_rows=len(frame),
                    headers=headers,
                    feature_columns=list(features.FEATURE_COLUMNS),
                    suggested_mapping=suggested,
                    n_signals_matched=sum(1 for v in suggested.values() if v is not None),
                )

            try:
                provided = json.loads(mapping)
                if not isinstance(provided, dict):
                    raise ValueError("mapping must be a JSON object")
            except (json.JSONDecodeError, ValueError) as exc:
                raise _upload.UploadError(f"invalid mapping payload: {exc}") from exc

            resolved = _upload.resolve_mapping(headers, provided)
            X = _upload.build_frame(frame, resolved)
            _upload.assert_scorable(X, resolved)
        except _upload.UploadError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        version, proba = _score_frame(X)
        mapped = _upload.mapped_signals(resolved)
        summary = _upload.summarize(proba, n_signals_provided=len(mapped))
        return UploadScoreResponse(
            model_version=version,
            n_rows=len(proba),
            n_signals_provided=len(mapped),
            mapped_signals=mapped,
            unmapped_signals=[s for s in features.FEATURE_COLUMNS if s not in mapped],
            failure_probability=proba,
            summary=UploadSummary(
                threshold=summary.threshold,
                n_high_risk=summary.n_high_risk,
                pct_high_risk=summary.pct_high_risk,
                histogram=summary.histogram,
                bin_edges=summary.bin_edges,
            ),
            demo=_is_demo_version(version),
        )

    # --- generate-your-own-data (F14a) ---------------------------------------
    #
    # The API's entire role here is to ENQUEUE. It writes a `queued` run, asks the worker
    # — a separate deployable unit — to execute it, and answers 202. It does not import
    # the forge, does not call generate.run_generation, and does not use BackgroundTasks
    # (ADR-026 / decision S2: that would collapse the system back to one container and
    # hollow out both remaining infra gates). A test asserts the API never generates.

    def _require_generation() -> tuple[store_gen.GenerationStore, _jobs.JobTrigger]:
        """Both halves of the split, or an honest 503 saying which one is missing."""
        if gen_store is None or trigger is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "generate-your-own-data is not configured on this deployment: it needs "
                    "a database (DATABASE_URL) to share state with the generation worker, "
                    "and a worker to run it (GENERATION_JOB, or GENERATION_LOCAL_WORKER=1 "
                    "for local development). The rest of the demo works without it."
                ),
            )
        return gen_store, trigger

    def _get_run(run_id: str) -> store_gen.GenerationRun:
        gs, _ = _require_generation()
        run = gs.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"no such generation run: {run_id}")
        return run

    def _run_response(run: store_gen.GenerationRun, *, worker: str | None = None) -> GenerationRunResponse:
        return GenerationRunResponse(
            run_id=run.run_id,
            status=run.status,
            n_units=run.n_units,
            days=run.days,
            seed=run.seed,
            n_rows=run.n_rows,
            expected_rows=run.spec.expected_rows,
            error=run.error,
            worker=worker,
        )

    @app.post("/demo/generate", response_model=GenerationRunResponse, status_code=202)
    def demo_generate(request: GenerateRequest) -> GenerationRunResponse:
        """Kick off a bounded fleet generation → **202** with a run id to poll.

        Returns immediately: the forge runs in the worker, not here. An out-of-bounds
        request (:class:`generate.CapExceeded`) is a **400** naming the cap it broke — the
        free-tier envelope is enforced at the door, never by letting a container wedge.
        """
        gs, trig = _require_generation()
        try:
            spec = _generate.GenerationSpec(
                n_units=request.n_units, days=request.days, seed=request.seed
            ).validate()
        except _generate.CapExceeded as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        run = gs.create_run(spec)
        try:
            trig.trigger(run.run_id, spec)
        except _jobs.TriggerError as exc:
            # The run row already exists, so the failure is visible where the user is
            # looking (the poll), not only in a server log.
            gs.mark_failed(run.run_id, error=str(exc))
            raise HTTPException(
                status_code=503, detail=f"could not start the generation worker: {exc}"
            ) from exc
        return _run_response(run, worker=trig.name)

    @app.get("/demo/generate/{run_id}", response_model=GenerationRunResponse)
    def demo_generate_status(run_id: str) -> GenerationRunResponse:
        """Poll a run: ``queued`` → ``running`` → ``succeeded`` / ``failed`` (with a reason)."""
        return _run_response(_get_run(run_id), worker=trigger.name if trigger else None)

    @app.get("/demo/generate/{run_id}/rows", response_model=GeneratedRows)
    def demo_generate_rows(run_id: str, offset: int = 0, limit: int = 50) -> GeneratedRows:
        """Browse the stored fleet, a page at a time (in time order, per vehicle)."""
        gs, _ = _require_generation()
        run = _get_run(run_id)
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        return GeneratedRows(
            run_id=run.run_id,
            total_rows=gs.count_readings(run_id),
            offset=offset,
            limit=limit,
            columns=_generate.stored_columns(),
            rows=gs.readings(run_id, offset=offset, limit=limit),
        )

    @app.get("/demo/generate/{run_id}/report", response_model=FleetReport)
    def demo_generate_report(run_id: str) -> FleetReport:
        """The per-vehicle risk roll-up over the generated fleet.

        Scored by whatever model is **promoted right now** — not by a score baked in at
        generation time. That is what keeps the registry's central property true here too:
        promote or roll back, and the report changes with no redeploy (ADR-008/ADR-009).
        The roll-up is cached per ``(run, model version)``, so the scoring cost is paid once
        per model and a promotion correctly invalidates it.
        """
        gs, _ = _require_generation()
        run = _get_run(run_id)
        if run.status != store_gen.STATUS_SUCCEEDED:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"run {run_id} is '{run.status}', not '{store_gen.STATUS_SUCCEEDED}' — "
                    "there is nothing to report on yet."
                    + (f" ({run.error})" if run.error else "")
                ),
            )
        try:
            version = store.load().version
        except LookupError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        units = gs.load_report(run_id, version)
        n_rows_scored = sum(u.n_rows for u in units)
        if not units:
            frame = gs.readings_frame(run_id)
            if frame.empty:
                raise HTTPException(
                    status_code=409, detail=f"run {run_id} stored no readings."
                )
            version, proba = _score_frame(frame.loc[:, list(features.FEATURE_COLUMNS)])
            units = _generate.roll_up(frame, np.asarray(proba))
            gs.save_report(run_id, version, units)
            n_rows_scored = len(frame)

        return FleetReport(
            run_id=run_id,
            model_version=version,
            demo=_is_demo_version(version),
            n_units=len(units),
            n_flagged=sum(1 for u in units if u.flagged),
            n_rows_scored=n_rows_scored,
            flag_threshold=_generate.FLAG_THRESHOLD,
            rollup_rule=(
                f"peak of a {_generate.ROLLUP_WINDOW_H:g}-hour rolling mean of the per-row "
                "failure probability (sustained risk, not a single spike)"
            ),
            units=[
                UnitRiskOut(
                    unit_id=u.unit_id,
                    n_rows=u.n_rows,
                    risk=u.risk,
                    peak=u.peak,
                    high_risk_share=u.high_risk_share,
                    flagged=u.flagged,
                )
                for u in units
            ],
        )

    @app.get("/demo", response_class=HTMLResponse)
    def demo() -> HTMLResponse:
        """A self-contained 'set parameters → get a prediction' page (F7).

        No CDN, no external asset — inline CSS/JS only (the clean-room/offline
        discipline). Shows the same ``demo=fixture`` honesty banner the rest of the
        surface carries, a form seeded with the feature signal names, and the recent
        predictions read back from the managed DB (empty when none is configured).
        """
        recent = log.recent(limit=10) if log is not None else []
        return HTMLResponse(
            _render_demo_page(
                recent,
                persistence=log is not None,
                # Generation needs BOTH halves of the split; if either is missing the panel
                # says so rather than offering a button that can only 503.
                generation=gen_store is not None and trigger is not None,
            )
        )

    return app


def config_version() -> str:
    """The package version, for the OpenAPI doc (kept out of the import cycle)."""
    from . import __version__

    return __version__


# --- the demo page ------------------------------------------------------------

# Per-signal metadata that turns the raw J1939 fields into *friendly* inputs (F9):
# a plain-language label, a unit, a short tooltip, and a bounded [min, max, step]
# range so a domain-naive tester sets sane values on a slider instead of guessing
# free-text. The keys stay the literal signal names (the API/scoring contract);
# only the presentation is friendly. i18n localizes label+tooltip on top of this.
_SIGNAL_META: dict[str, dict[str, object]] = {
    "engine_speed_rpm": {"unit": "rpm", "min": 600, "max": 2500, "step": 10},
    "coolant_temp_c": {"unit": "°C", "min": 60, "max": 160, "step": 1},
    "oil_pressure_kpa": {"unit": "kPa", "min": 0, "max": 550, "step": 5},
    "engine_load_pct": {"unit": "%", "min": 0, "max": 100, "step": 1},
    "fuel_rate_lph": {"unit": "L/h", "min": 0, "max": 220, "step": 1},
    "boost_pressure_kpa": {"unit": "kPa", "min": 0, "max": 300, "step": 5},
    "egt_c": {"unit": "°C", "min": 200, "max": 760, "step": 5},
    "def_level_pct": {"unit": "%", "min": 0, "max": 100, "step": 1},
    "vibration_mms": {"unit": "mm/s", "min": 0, "max": 30, "step": 0.1},
}

# One-click preset rows so a tester can try a "healthy" or a "failing" machine in a
# single click, without knowing what a plausible value is (the biggest UX win, F9.1).
#
# One preset per failure mode the generator models (overheat / oil_starve / bearing),
# so the demo shows real, distinct probabilities across the modes the baked model was
# trained on — see ADR-019. Each "failing" preset is grounded in a REAL near-event
# operating point from the multi-mode smoke fixture (the exact row the baked model
# scores near-1 for that mode), lightly rounded; "healthy" is a plausible loaded-but-
# fine machine the model scores near-0. They are illustrative operating points for the
# *wired endpoint*, never a reported number (the ≈0.82 metric is the full-data model).
#
# ``oil_starve`` deliberately leaves egt/def/vibration as ``None`` (era-NULL): in this
# fleet oil-starvation strikes an older equipment era that physically lacks those
# sensors, so a faithful oil-starve row has them blank — a legitimate model input
# (era-NULL, ADR-008 upstream). The demo form clears those fields for this preset.
_PRESETS: dict[str, dict[str, float | None]] = {
    "healthy": {  # a loaded machine running normally — model scores this ~0
        "engine_speed_rpm": 1500,
        "coolant_temp_c": 88,
        "oil_pressure_kpa": 340,
        "engine_load_pct": 55,
        "fuel_rate_lph": 90,
        "boost_pressure_kpa": 140,
        "egt_c": 470,
        "def_level_pct": 80,
        "vibration_mms": 4,
    },
    "overheat": {  # thermal event: coolant + EGT high under load
        "engine_speed_rpm": 1700,
        "coolant_temp_c": 137,
        "oil_pressure_kpa": 275,
        "engine_load_pct": 79,
        "fuel_rate_lph": 136,
        "boost_pressure_kpa": 190,
        "egt_c": 660,
        "def_level_pct": 13,
        "vibration_mms": 12.6,
    },
    "oil_starve": {  # oil pressure collapsing under load (older era: no egt/def/vib)
        "engine_speed_rpm": 1860,
        "coolant_temp_c": 123,
        "oil_pressure_kpa": 55,
        "engine_load_pct": 88,
        "fuel_rate_lph": 168,
        "boost_pressure_kpa": 170,
        "egt_c": None,
        "def_level_pct": None,
        "vibration_mms": None,
    },
    "bearing": {  # mechanical wear: high vibration under load
        "engine_speed_rpm": 1610,
        "coolant_temp_c": 104,
        "oil_pressure_kpa": 230,
        "engine_load_pct": 66,
        "fuel_rate_lph": 115,
        "boost_pressure_kpa": 100,
        "egt_c": 525,
        "def_level_pct": 20,
        "vibration_mms": 20.2,
    },
}

# The healthy preset is the form's seed (a first visitor can hit Predict immediately).
# It is fully populated (no era-NULL), so every slider starts with a sane value.
_DEMO_SEED: dict[str, float | None] = _PRESETS["healthy"]


def _render_demo_page(
    recent: list[store_pg.LoggedPrediction],
    *,
    persistence: bool,
    generation: bool = False,
) -> str:
    """Render the self-contained demo HTML (inline CSS/JS, no external asset).

    Friendly inputs (F9): each :data:`features.FEATURE_COLUMNS` signal is a bounded,
    unit-labelled, tooltipped field seeded from the healthy preset; one-click presets
    fill a whole plausible row; the result is a failure **probability** rendered as a
    labelled meter with a plain-language risk band (the number carries the meaning,
    colour is a redundant cue). Light/dark theme + EN/PT-BR i18n are self-contained
    (no CDN). The ``demo=fixture`` honesty banner + ≈0.82 framing are intact in **both**
    languages. The recent-predictions panel reads from the managed DB (Neon, F7) when
    configured, else a short "logging off" note.

    The signal metadata, presets and locale strings are injected as **JSON** (not
    format-substituted HTML) so the tiny inline JS builds the form/i18n from data — this
    keeps the ``str.format`` template free of the brace-doubling that raw data would need.
    """
    import html

    signal_meta = {name: _SIGNAL_META[name] for name in features.FEATURE_COLUMNS}

    # Recent predictions rows (server-rendered), or the logging-off note. The chrome
    # around them is localized client-side; the data itself is language-neutral.
    if persistence:
        if recent:
            rows = "\n".join(
                f"<tr><td>{html.escape(r.created_at.strftime('%Y-%m-%d %H:%M:%S'))} UTC</td>"
                f"<td>v{html.escape(r.model_version)}</td>"
                f"<td>{r.failure_probability:.3f}</td></tr>"
                for r in recent
            )
            recent_html = (
                "<table class='recent'><thead><tr><th data-i18n='colWhen'></th>"
                "<th data-i18n='colModel'></th><th data-i18n='colProb'></th></tr>"
                f"</thead><tbody>{rows}</tbody></table>"
            )
        else:
            recent_html = "<p class='muted' data-i18n='noneLogged'></p>"
        recent_note_key = "loggingOn"
    else:
        recent_html = ""
        recent_note_key = "loggingOff"

    # ``ensure_ascii=False`` keeps real UTF-8 (°, ≈, PT-BR accents) in the source — the
    # page is served as UTF-8 (<meta charset>), so this is cleaner than \uXXXX escapes.
    # The caps are injected, never duplicated in the page: `generate` owns them, the form's
    # min/max/step come from there, and the server re-validates anyway (a browser bound is
    # a hint, not a guard).
    gen_caps = {
        "min_units": _generate.MIN_UNITS,
        "max_units": _generate.MAX_UNITS,
        "min_days": _generate.MIN_DAYS,
        "max_days": _generate.MAX_DAYS,
        "max_unit_days": _generate.MAX_UNIT_DAYS,
        "default_units": _generate.DEFAULT_UNITS,
        "default_days": _generate.DEFAULT_DAYS,
        "default_seed": config.DEFAULT_SEED,
    }
    return _DEMO_TEMPLATE.format(
        signal_meta_json=json.dumps(signal_meta, ensure_ascii=False),
        feature_order_json=json.dumps(list(features.FEATURE_COLUMNS), ensure_ascii=False),
        presets_json=json.dumps(_PRESETS, ensure_ascii=False),
        i18n_json=json.dumps(_DEMO_I18N, ensure_ascii=False),
        gen_caps_json=json.dumps(gen_caps),
        gen_enabled_json=json.dumps(bool(generation)),
        recent_html=recent_html,
        recent_note_key=recent_note_key,
    )


# The locale strings for the /demo page (F9 i18n), injected as JSON and consumed by
# the inline JS. Covers the UI SHELL ONLY — the chrome, the friendly signal labels +
# tooltips, and the preset names. **Honesty boundary held in both languages** (ADR-018):
# the `demo=fixture` banner and the ≈0.82 "reported result" framing are translated in
# meaning, not softened; i18n localizes the interface, not the model/output semantics.
# `sig`/`tip` are keyed by the literal signal name (which stays the API contract).
_DEMO_I18N: dict[str, dict[str, object]] = {
    "en": {
        "langName": "EN",
        "toDark": "☾ Dark",
        "toLight": "☀ Light",
        "title": "forge-pdm-mlops — try a prediction",
        "sub": "Set the engine's sensor readings, get the model's failure probability.",
        "banner": (
            "<strong>DEMO model.</strong> The served model is trained on a small committed "
            "<em>smoke fixture</em> — its probabilities illustrate the wired endpoint, they "
            "are <strong>not</strong> a reported result. The real ≈0.82 ROC-AUC model is "
            "trained on the full dataset locally (see <a href='/model-info'>/model-info</a>)."
        ),
        "presetsLabel": "Try an example:",
        "presetHealthy": "Healthy engine",
        "presetOverheat": "Overheating",
        "presetOilStarve": "Oil starvation",
        "presetBearing": "Failing bearing",
        "predict": "Predict",
        "scoring": "scoring…",
        "failureProb": "Failure probability",
        "riskLow": "low risk",
        "riskModerate": "moderate risk",
        "riskHigh": "high risk",
        "modelV": "model v",
        "logged": "logged",
        "predictFailed": (
            "Prediction failed: {msg} (is a model promoted? see <a href='/health'>/health</a>)"
        ),
        "byodTitle": "Bring your own data",
        "byodBlurb": (
            "Upload a CAN/J1939 batch (<code>.csv</code> or <code>.parquet</code>) to score "
            "every row. Your column names don't have to match ours — you'll map them in the "
            "next step, and any signal you leave unmapped is treated as a missing sensor "
            "(era-NULL), so a partial dataset still scores. Same <strong>DEMO</strong> model "
            "as above; nothing you upload is stored."
        ),
        "parsing": "parsing…",
        "rows": "rows",
        "signalsAutoMatched": "signals auto-matched",
        "of": "of",
        "uploadFailed": "Upload failed: {msg}",
        "scoringFailed": "Scoring failed: {msg}",
        "expectedSignal": "expected signal",
        "yourColumn": "your column",
        "leaveEmpty": "— (leave empty · era-NULL) —",
        "autoMatched": "auto-matched",
        "noMatch": "no match — map or leave empty",
        "scoreBatch": "Score batch",
        "batchBanner": (
            "<strong>DEMO model.</strong> Your batch was scored by the fixture-trained demo "
            "model (see <a href='/model-info'>/model-info</a>) — illustrative, not a reported "
            "result. Nothing you uploaded was stored."
        ),
        "rowsScored": "rows scored",
        "signalsProvided": "signals provided",
        "atRisk": "≥ {t}% risk ({n} rows)",
        "missingEraNull": "Missing signals scored as era-NULL: {list}",
        "showingFirst": "Showing the first {n} of {total} rows.",
        "axisProb": "failure probability →",
        "colRow": "row",
        "colProb": "failure prob.",
        "genTitle": "Generate your own fleet",
        "genBlurb": (
            "Above you scored <em>your</em> data. Here you can <strong>make some</strong>: the "
            "companion generator builds a synthetic fleet of heavy machinery, and every "
            "vehicle in it gets a risk score. Generation runs in a <strong>separate worker "
            "service</strong> — this page only kicks it off and polls for the result."
        ),
        "genOff": (
            "Fleet generation is <strong>off</strong> on this deployment (it needs a database "
            "and a worker) — it runs on the Cloud Run deploy."
        ),
        "genUnits": "Vehicles",
        "genDays": "Window (days)",
        "genSeed": "Seed",
        "genGo": "Generate fleet",
        "genCapHint": "Up to {cap} unit-days (vehicles × days) — a free-tier storage cap.",
        "genQueued": "queued — waiting for the worker…",
        "genRunning": "generating on the worker…",
        "genFailed": "Generation failed: {msg}",
        "genWorker": "worker: {name}",
        "genReportTitle": "Fleet risk report",
        "genFlagged": "vehicles flagged",
        "genOf": "of",
        "genRowsGen": "readings generated",
        "genRule": (
            "A vehicle is flagged when its risk reaches {t}%. Risk is the <strong>peak of a "
            "1-hour rolling mean</strong> of its per-reading failure probability — a "
            "<em>sustained</em> elevation, not a single spike. (Ranking on the single highest "
            "reading instead would flag healthy vehicles: one injected sensor glitch is enough.)"
        ),
        "genDemo": (
            "<strong>DEMO model.</strong> This fleet was scored by the fixture-trained demo "
            "model (see <a href='/model-info'>/model-info</a>) — illustrative, not a reported "
            "result."
        ),
        "genColUnit": "vehicle",
        "genColRisk": "risk (sustained)",
        "genColPeak": "peak reading",
        "genColShare": "high-risk readings",
        "genFlag": "flagged",
        "genOk": "ok",
        "genBrowseTitle": "The generated data",
        "genShowing": "Showing the first {n} of {total} stored readings.",
        "recentTitle": "Recent predictions",
        "colWhen": "when",
        "colModel": "model",
        "noneLogged": "No predictions logged yet — submit one above.",
        "loggingOn": "Logged to a managed <strong>Postgres</strong> instance (Neon).",
        "loggingOff": (
            "Prediction logging is <strong>off</strong> (no <code>DATABASE_URL</code>) — "
            "the managed-DB panel appears on the Cloud Run deploy."
        ),
        "i18nNote": (
            "This interface is available in English and Portuguese; the model and its output "
            "are unchanged — i18n localizes the UI, not the prediction semantics."
        ),
        "sig": {
            "engine_speed_rpm": "Engine speed",
            "coolant_temp_c": "Coolant temperature",
            "oil_pressure_kpa": "Oil pressure",
            "engine_load_pct": "Engine load",
            "fuel_rate_lph": "Fuel rate",
            "boost_pressure_kpa": "Boost pressure",
            "egt_c": "Exhaust gas temperature",
            "def_level_pct": "DEF level",
            "vibration_mms": "Vibration",
        },
        "tip": {
            "engine_speed_rpm": "Crankshaft speed. Idle ≈ 700, working ≈ 1800.",
            "coolant_temp_c": "Engine coolant. Normal ≈ 85–95; a sustained climb signals overheating.",
            "oil_pressure_kpa": "Lubrication pressure. Healthy ≈ 300+; a slow drop hints at bearing wear.",
            "engine_load_pct": "How hard the engine is working, 0–100%.",
            "fuel_rate_lph": "Fuel consumption in litres per hour.",
            "boost_pressure_kpa": "Turbocharger boost above atmospheric.",
            "egt_c": "Exhaust gas temperature; runs high under heavy load or a thermal fault.",
            "def_level_pct": "Diesel exhaust fluid (AdBlue) tank level, 0–100%.",
            "vibration_mms": "Chassis/bearing vibration; a spike is the bearing-failure signature.",
        },
    },
    "pt-BR": {
        "langName": "PT",
        "toDark": "☾ Escuro",
        "toLight": "☀ Claro",
        "title": "forge-pdm-mlops — teste uma previsão",
        "sub": "Ajuste as leituras dos sensores do motor e veja a probabilidade de falha do modelo.",
        "banner": (
            "<strong>Modelo DEMO.</strong> O modelo servido é treinado num pequeno "
            "<em>fixture de fumaça</em> versionado — suas probabilidades ilustram o endpoint "
            "conectado, <strong>não</strong> são um resultado reportado. O modelo real de "
            "ROC-AUC ≈0.82 é treinado no conjunto completo localmente "
            "(veja <a href='/model-info'>/model-info</a>)."
        ),
        "presetsLabel": "Teste um exemplo:",
        "presetHealthy": "Motor saudável",
        "presetOverheat": "Superaquecimento",
        "presetOilStarve": "Falta de óleo",
        "presetBearing": "Rolamento falhando",
        "predict": "Prever",
        "scoring": "pontuando…",
        "failureProb": "Probabilidade de falha",
        "riskLow": "risco baixo",
        "riskModerate": "risco moderado",
        "riskHigh": "risco alto",
        "modelV": "modelo v",
        "logged": "registrado",
        "predictFailed": (
            "Falha na previsão: {msg} (há um modelo promovido? veja "
            "<a href='/health'>/health</a>)"
        ),
        "byodTitle": "Traga seus próprios dados",
        "byodBlurb": (
            "Envie um lote CAN/J1939 (<code>.csv</code> ou <code>.parquet</code>) para pontuar "
            "cada linha. Os nomes das suas colunas não precisam coincidir com os nossos — você "
            "os mapeia na próxima etapa, e qualquer sinal deixado sem mapeamento é tratado como "
            "sensor ausente (era-NULL), então um conjunto parcial ainda é pontuado. Mesmo modelo "
            "<strong>DEMO</strong> acima; nada que você enviar é armazenado."
        ),
        "parsing": "lendo…",
        "rows": "linhas",
        "signalsAutoMatched": "sinais mapeados automaticamente",
        "of": "de",
        "uploadFailed": "Falha no envio: {msg}",
        "scoringFailed": "Falha na pontuação: {msg}",
        "expectedSignal": "sinal esperado",
        "yourColumn": "sua coluna",
        "leaveEmpty": "— (deixar vazio · era-NULL) —",
        "autoMatched": "mapeado automaticamente",
        "noMatch": "sem correspondência — mapeie ou deixe vazio",
        "scoreBatch": "Pontuar lote",
        "batchBanner": (
            "<strong>Modelo DEMO.</strong> Seu lote foi pontuado pelo modelo demo treinado no "
            "fixture (veja <a href='/model-info'>/model-info</a>) — ilustrativo, não um "
            "resultado reportado. Nada do que você enviou foi armazenado."
        ),
        "rowsScored": "linhas pontuadas",
        "signalsProvided": "sinais fornecidos",
        "atRisk": "≥ {t}% de risco ({n} linhas)",
        "missingEraNull": "Sinais ausentes pontuados como era-NULL: {list}",
        "showingFirst": "Mostrando as primeiras {n} de {total} linhas.",
        "axisProb": "probabilidade de falha →",
        "colRow": "linha",
        "colProb": "prob. de falha",
        "genTitle": "Gere a sua própria frota",
        "genBlurb": (
            "Acima você pontuou os <em>seus</em> dados. Aqui você pode <strong>criá-los</strong>: "
            "o gerador companheiro monta uma frota sintética de máquinas pesadas, e cada veículo "
            "dela recebe um score de risco. A geração roda num <strong>serviço worker "
            "separado</strong> — esta página apenas dispara e consulta o resultado."
        ),
        "genOff": (
            "A geração de frota está <strong>desligada</strong> neste deploy (precisa de um "
            "banco e de um worker) — ela roda no deploy do Cloud Run."
        ),
        "genUnits": "Veículos",
        "genDays": "Janela (dias)",
        "genSeed": "Semente",
        "genGo": "Gerar frota",
        "genCapHint": "Até {cap} unidade-dias (veículos × dias) — um teto de armazenamento do free tier.",
        "genQueued": "na fila — aguardando o worker…",
        "genRunning": "gerando no worker…",
        "genFailed": "A geração falhou: {msg}",
        "genWorker": "worker: {name}",
        "genReportTitle": "Relatório de risco da frota",
        "genFlagged": "veículos sinalizados",
        "genOf": "de",
        "genRowsGen": "leituras geradas",
        "genRule": (
            "Um veículo é sinalizado quando seu risco atinge {t}%. O risco é o <strong>pico de "
            "uma média móvel de 1 hora</strong> da probabilidade de falha por leitura — uma "
            "elevação <em>sustentada</em>, não um pico isolado. (Ranquear pela maior leitura "
            "isolada sinalizaria veículos saudáveis: basta uma falha de sensor injetada.)"
        ),
        "genDemo": (
            "<strong>Modelo DEMO.</strong> Esta frota foi pontuada pelo modelo demo treinado no "
            "fixture (veja <a href='/model-info'>/model-info</a>) — ilustrativo, não é um "
            "resultado reportado."
        ),
        "genColUnit": "veículo",
        "genColRisk": "risco (sustentado)",
        "genColPeak": "pico de leitura",
        "genColShare": "leituras de alto risco",
        "genFlag": "sinalizado",
        "genOk": "ok",
        "genBrowseTitle": "Os dados gerados",
        "genShowing": "Mostrando as primeiras {n} de {total} leituras armazenadas.",
        "recentTitle": "Previsões recentes",
        "colWhen": "quando",
        "colModel": "modelo",
        "noneLogged": "Nenhuma previsão registrada ainda — envie uma acima.",
        "loggingOn": "Registrado num <strong>Postgres</strong> gerenciado (Neon).",
        "loggingOff": (
            "O registro de previsões está <strong>desligado</strong> (sem "
            "<code>DATABASE_URL</code>) — o painel do banco gerenciado aparece no deploy "
            "do Cloud Run."
        ),
        "i18nNote": (
            "Esta interface está disponível em inglês e português; o modelo e sua saída não "
            "mudam — a i18n localiza a UI, não a semântica da previsão."
        ),
        "sig": {
            "engine_speed_rpm": "Rotação do motor",
            "coolant_temp_c": "Temperatura do líquido de arrefecimento",
            "oil_pressure_kpa": "Pressão do óleo",
            "engine_load_pct": "Carga do motor",
            "fuel_rate_lph": "Consumo de combustível",
            "boost_pressure_kpa": "Pressão do turbo",
            "egt_c": "Temperatura dos gases de escape",
            "def_level_pct": "Nível de ARLA (DEF)",
            "vibration_mms": "Vibração",
        },
        "tip": {
            "engine_speed_rpm": "Rotação do virabrequim. Marcha lenta ≈ 700, trabalhando ≈ 1800.",
            "coolant_temp_c": "Arrefecimento do motor. Normal ≈ 85–95; subida contínua indica superaquecimento.",
            "oil_pressure_kpa": "Pressão de lubrificação. Saudável ≈ 300+; queda lenta sugere desgaste de rolamento.",
            "engine_load_pct": "O quão exigido está o motor, 0–100%.",
            "fuel_rate_lph": "Consumo de combustível em litros por hora.",
            "boost_pressure_kpa": "Pressão do turbo acima da atmosférica.",
            "egt_c": "Temperatura dos gases de escape; sobe sob carga pesada ou falha térmica.",
            "def_level_pct": "Nível do tanque de ARLA (AdBlue), 0–100%.",
            "vibration_mms": "Vibração do chassi/rolamento; um pico é a assinatura de falha de rolamento.",
        },
    },
}


# Inline CSS/JS only (clean-room / offline / Artifact-CSP-safe by the same discipline).
# Light/dark theme via CSS custom props (prefers-color-scheme + a persisted `data-theme`
# override that wins in both directions); EN/PT-BR i18n from the injected `I18N` JSON.
# The JS builds the friendly form from `SIGNAL_META`, fills a whole row from `PRESETS`,
# posts to /demo/predict, and renders the returned probability as a labelled meter — the
# number + risk word are the primary encoding, the bar colour a redundant cue.

_DEMO_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>forge-pdm-mlops — try a prediction</title>
<style>
  /* Light is the default; dark applies via prefers-color-scheme AND a persisted
     `data-theme` on <html> that wins in both directions (self-contained, no CDN). */
  :root {{ --ink:#1a2233; --muted:#5b6472; --line:#e2e6ec; --surface:#f7f8fa;
           --bg:#ffffff; --accent:#3b5bdb; --demo:#8a5a00; --demo-bg:#fff6e0;
           --demo-line:#f0d9a8; --err:#c92a2a; --meter-bg:#f7f8fa; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --ink:#e7e9ee; --muted:#9aa3b2; --line:#2b3340; --surface:#1f2530;
             --bg:#0f1115; --accent:#38bdf8; --demo:#f4c65a; --demo-bg:#2a2312;
             --demo-line:#4a3d17; --err:#f87171; --meter-bg:#1f2530; }}
  }}
  :root[data-theme="dark"] {{ --ink:#e7e9ee; --muted:#9aa3b2; --line:#2b3340;
    --surface:#1f2530; --bg:#0f1115; --accent:#38bdf8; --demo:#f4c65a;
    --demo-bg:#2a2312; --demo-line:#4a3d17; --err:#f87171; --meter-bg:#1f2530; }}
  :root[data-theme="light"] {{ --ink:#1a2233; --muted:#5b6472; --line:#e2e6ec;
    --surface:#f7f8fa; --bg:#ffffff; --accent:#3b5bdb; --demo:#8a5a00;
    --demo-bg:#fff6e0; --demo-line:#f0d9a8; --err:#c92a2a; --meter-bg:#f7f8fa; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
          color:var(--ink); background:var(--bg); }}
  main {{ max-width:820px; margin:0 auto; padding:24px 20px 64px; }}
  .top {{ display:flex; justify-content:space-between; align-items:flex-start; gap:1rem; }}
  h1 {{ font-size:1.5rem; margin:.2rem 0 .1rem; }}
  .sub {{ color:var(--muted); margin:0 0 20px; }}
  .controls {{ display:flex; gap:.4rem; flex-shrink:0; }}
  .toggle {{ background:var(--surface); color:var(--muted); border:1px solid var(--line);
    border-radius:999px; padding:.3rem .7rem; font-size:.78rem; cursor:pointer;
    line-height:1; white-space:nowrap; }}
  .toggle:hover {{ border-color:var(--accent); color:var(--ink); }}
  .banner {{ background:var(--demo-bg); color:var(--demo); border:1px solid var(--demo-line);
             border-radius:8px; padding:10px 14px; font-size:.9rem; margin-bottom:22px; }}
  .banner a {{ color:var(--demo); }}
  .presets {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:0 0 16px; }}
  .presets .label {{ font-size:.8rem; color:var(--muted); }}
  .preset {{ background:var(--surface); color:var(--ink); border:1px solid var(--line);
    border-radius:999px; padding:.4rem .9rem; font-size:.85rem; cursor:pointer; }}
  .preset:hover {{ border-color:var(--accent); }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(160px,1fr));
           gap:12px; }}
  .field {{ display:flex; flex-direction:column; gap:4px; font-size:.82rem;
            color:var(--muted); }}
  .field .lab {{ display:flex; align-items:center; gap:4px; }}
  .field .unit {{ color:var(--muted); opacity:.8; }}
  .field .info {{ display:inline-flex; align-items:center; justify-content:center;
    width:14px; height:14px; border-radius:50%; border:1px solid var(--line);
    font-size:.62rem; color:var(--muted); cursor:help; flex-shrink:0; }}
  .field input {{ font-size:.95rem; padding:7px 9px; border:1px solid var(--line);
                  border-radius:6px; color:var(--ink); background:var(--bg); }}
  .field input:focus {{ outline:none; border-color:var(--accent); }}
  .actions {{ margin:20px 0; display:flex; gap:12px; align-items:center; }}
  button.go {{ background:var(--accent); color:#fff; border:0; border-radius:8px;
            padding:10px 20px; font-size:.95rem; font-weight:600; cursor:pointer; }}
  button.go:disabled {{ opacity:.5; cursor:default; }}
  #result {{ margin-top:8px; }}
  .meter-wrap {{ display:flex; flex-direction:column; gap:6px; max-width:520px; }}
  .meter {{ height:14px; background:var(--meter-bg); border:1px solid var(--line);
            border-radius:7px; overflow:hidden; }}
  .meter > i {{ display:block; height:100%; border-radius:7px 0 0 7px;
                transition:width .3s; }}
  .headline {{ font-size:1.05rem; }}
  .headline b {{ font-size:1.4rem; }}
  .muted {{ color:var(--muted); }}
  section {{ margin-top:34px; }}
  h2 {{ font-size:1.05rem; }}
  table.recent {{ width:100%; border-collapse:collapse; font-size:.88rem; }}
  table.recent th, table.recent td {{ text-align:left; padding:7px 10px;
    border-bottom:1px solid var(--line); }}
  table.recent th {{ color:var(--muted); font-weight:600; }}
  code {{ background:var(--surface); padding:1px 5px; border-radius:4px; }}
  a {{ color:var(--accent); }}
  .drop {{ border:1.5px dashed var(--line); border-radius:8px; padding:16px;
           background:var(--surface); }}
  .drop input[type=file] {{ font-size:.9rem; color:var(--ink); }}
  table.map {{ width:100%; border-collapse:collapse; font-size:.86rem; margin-top:14px; }}
  table.map th, table.map td {{ text-align:left; padding:6px 8px;
    border-bottom:1px solid var(--line); vertical-align:middle; }}
  table.map th {{ color:var(--muted); font-weight:600; }}
  table.map select {{ font-size:.86rem; padding:5px 7px; border:1px solid var(--line);
    border-radius:6px; color:var(--ink); background:var(--bg); max-width:100%; }}
  .conf {{ font-size:.78rem; color:var(--muted); }}
  .summary {{ display:flex; flex-wrap:wrap; gap:10px 22px; margin:12px 0; }}
  .stat {{ display:flex; flex-direction:column; }}
  .stat b {{ font-size:1.25rem; }}
  .stat span {{ font-size:.78rem; color:var(--muted); }}
  .hist {{ display:flex; align-items:flex-end; gap:3px; height:90px; margin:10px 0;
    max-width:520px; }}
  .hist .bar {{ flex:1; background:var(--accent); border-radius:3px 3px 0 0; min-height:2px;
    opacity:.85; }}
  .hist-axis {{ display:flex; justify-content:space-between; max-width:520px;
    font-size:.72rem; color:var(--muted); }}
  table.rows {{ width:100%; border-collapse:collapse; font-size:.84rem; margin-top:10px; }}
  table.rows th, table.rows td {{ text-align:left; padding:5px 9px;
    border-bottom:1px solid var(--line); }}
  table.rows th {{ color:var(--muted); font-weight:600; }}
  .i18n-note {{ margin-top:30px; font-size:.76rem; color:var(--muted); }}
  .err {{ color:var(--err); }}
  table.fleet {{ width:100%; border-collapse:collapse; font-size:.86rem; margin-top:10px; }}
  table.fleet th, table.fleet td {{ text-align:left; padding:6px 9px;
    border-bottom:1px solid var(--line); }}
  table.fleet th {{ color:var(--muted); font-weight:600; }}
  table.fleet tr.flag td {{ background:var(--demo-bg); }}
  .pill {{ display:inline-block; padding:1px 8px; border-radius:999px; font-size:.72rem;
    font-weight:600; border:1px solid var(--line); color:var(--muted); }}
  .pill.on {{ background:var(--demo-bg); color:var(--demo); border-color:var(--demo-line); }}
  .bar-cell {{ display:flex; align-items:center; gap:7px; }}
  .bar-cell .track {{ flex:1; max-width:110px; height:7px; border-radius:4px;
    background:var(--meter-bg); overflow:hidden; }}
  .bar-cell .track > i {{ display:block; height:100%; background:var(--accent); }}
</style></head>
<body><main>
  <div class="top">
    <div>
      <h1 data-i18n="title"></h1>
      <p class="sub" data-i18n="sub"></p>
    </div>
    <div class="controls">
      <button class="toggle" id="themeBtn"></button>
      <button class="toggle" id="langBtn"></button>
    </div>
  </div>
  <div class="banner" data-i18n-html="banner"></div>

  <div class="presets">
    <span class="label" data-i18n="presetsLabel"></span>
    <button class="preset" data-preset="healthy" data-i18n="presetHealthy"></button>
    <button class="preset" data-preset="overheat" data-i18n="presetOverheat"></button>
    <button class="preset" data-preset="oil_starve" data-i18n="presetOilStarve"></button>
    <button class="preset" data-preset="bearing" data-i18n="presetBearing"></button>
  </div>

  <form id="f">
    <div class="grid" id="fields"></div>
    <div class="actions">
      <button type="submit" class="go" data-i18n="predict"></button>
      <span class="muted" id="status"></span>
    </div>
  </form>
  <div id="result"></div>

  <section>
    <h2 data-i18n="byodTitle"></h2>
    <p class="muted" data-i18n-html="byodBlurb"></p>
    <div class="drop">
      <input type="file" id="file" accept=".csv,.parquet">
      <span class="muted" id="upStatus"></span>
    </div>
    <div id="mapArea"></div>
    <div id="uploadResult"></div>
  </section>

  <section>
    <h2 data-i18n="genTitle"></h2>
    <p class="muted" data-i18n-html="genBlurb"></p>
    <p class="muted" id="genOff" data-i18n-html="genOff" hidden></p>
    <form id="genForm">
      <div class="grid" id="genFields"></div>
      <p class="conf" id="genCapHint"></p>
      <div class="actions">
        <button type="submit" class="go" data-i18n="genGo"></button>
        <span class="muted" id="genStatus"></span>
      </div>
    </form>
    <div id="genResult"></div>
  </section>

  <section>
    <h2 data-i18n="recentTitle"></h2>
    <p class="muted" data-i18n-html="{recent_note_key}"></p>
    {recent_html}
  </section>

  <p class="i18n-note" data-i18n="i18nNote"></p>

  <script>
  const SIGNAL_META = {signal_meta_json};
  const FEATURE_ORDER = {feature_order_json};
  const PRESETS = {presets_json};
  const I18N = {i18n_json};
  const GEN_CAPS = {gen_caps_json};
  const GEN_ENABLED = {gen_enabled_json};

  // --- theme: prefers-color-scheme default + persisted manual override -----------
  const THEME_KEY = 'forge-pdm.theme';
  function initialTheme() {{
    try {{ const s = localStorage.getItem(THEME_KEY);
      if (s === 'light' || s === 'dark') return s; }} catch (e) {{}}
    return (window.matchMedia &&
      window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
  }}
  let theme = initialTheme();
  function applyTheme() {{
    document.documentElement.setAttribute('data-theme', theme);
    try {{ localStorage.setItem(THEME_KEY, theme); }} catch (e) {{}}
    document.getElementById('themeBtn').textContent =
      theme === 'dark' ? t('toLight') : t('toDark');
  }}

  // --- i18n: browser-language default + persisted manual override ----------------
  const LANG_KEY = 'forge-pdm.lang';
  function initialLang() {{
    try {{ const s = localStorage.getItem(LANG_KEY);
      if (I18N[s]) return s; }} catch (e) {{}}
    return (navigator.language || 'en').toLowerCase().startsWith('pt') ? 'pt-BR' : 'en';
  }}
  let lang = initialLang();
  function t(key) {{ const d = I18N[lang] || I18N.en; return (d[key] != null ? d[key] : (I18N.en[key] != null ? I18N.en[key] : key)); }}
  function applyLang() {{
    try {{ localStorage.setItem(LANG_KEY, lang); }} catch (e) {{}}
    document.documentElement.setAttribute('lang', lang);
    document.getElementById('langBtn').textContent = lang === 'en' ? 'PT' : 'EN';
    // Text nodes and inner-HTML nodes tagged for translation.
    for (const el of document.querySelectorAll('[data-i18n]'))
      el.textContent = t(el.getAttribute('data-i18n'));
    for (const el of document.querySelectorAll('[data-i18n-html]'))
      el.innerHTML = t(el.getAttribute('data-i18n-html'));
    buildFields();          // labels/tooltips are localized → rebuild (values preserved)
    buildGenFields();       // …and so are the fleet-generation ones
    applyTheme();           // the theme button label is localized too
  }}

  const esc = (s) => String(s).replace(/[&<>"]/g, c =>
    ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}})[c]);

  // --- friendly form: one bounded, unit-labelled, tooltipped field per signal ----
  function currentValues() {{
    const v = {{}};
    for (const el of document.querySelectorAll('#fields input'))
      v[el.name] = el.value;
    return v;
  }}
  function buildFields() {{
    const prev = currentValues();
    const sig = (I18N[lang] || I18N.en).sig || {{}};
    const tip = (I18N[lang] || I18N.en).tip || {{}};
    let html = '';
    for (const name of FEATURE_ORDER) {{
      const m = SIGNAL_META[name] || {{}};
      const seed = PRESETS.healthy[name];
      const val = prev[name] !== undefined ? prev[name] : (seed !== undefined ? seed : '');
      const label = esc(sig[name] || name);
      const unit = m.unit ? ' <span class="unit">(' + esc(m.unit) + ')</span>' : '';
      const info = tip[name]
        ? ' <span class="info" title="' + esc(tip[name]) + '">i</span>' : '';
      html += '<label class="field"><span class="lab">' + label + unit + info +
        '</span><input type="number" name="' + esc(name) + '"' +
        (m.min !== undefined ? ' min="' + m.min + '"' : '') +
        (m.max !== undefined ? ' max="' + m.max + '"' : '') +
        (m.step !== undefined ? ' step="' + m.step + '"' : ' step="any"') +
        ' value="' + esc(val) + '"></label>';
    }}
    document.getElementById('fields').innerHTML = html;
  }}
  function fillPreset(name) {{
    const row = PRESETS[name]; if (!row) return;
    // A null preset value is era-NULL (a sensor this equipment era lacks) — clear the
    // field so the form submits null, a legitimate model input, instead of a stale value.
    for (const el of document.querySelectorAll('#fields input'))
      if (row[el.name] !== undefined) el.value = row[el.name] === null ? '' : row[el.name];
  }}

  document.getElementById('themeBtn').addEventListener('click', () => {{
    theme = theme === 'dark' ? 'light' : 'dark'; applyTheme();
  }});
  document.getElementById('langBtn').addEventListener('click', () => {{
    lang = lang === 'en' ? 'pt-BR' : 'en'; applyLang();
  }});
  for (const b of document.querySelectorAll('.preset'))
    b.addEventListener('click', () => fillPreset(b.dataset.preset));

  applyLang();  // builds fields + stamps theme/lang; run once after listeners wired

  // --- single-row predict --------------------------------------------------------
  const form = document.getElementById('f');
  const result = document.getElementById('result');
  const statusEl = document.getElementById('status');
  form.addEventListener('submit', async (e) => {{
    e.preventDefault();
    const btn = form.querySelector('button');
    btn.disabled = true; statusEl.textContent = t('scoring');
    const reading = {{}};
    for (const el of form.querySelectorAll('input')) {{
      reading[el.name] = el.value === '' ? null : parseFloat(el.value);
    }}
    try {{
      const resp = await fetch('/demo/predict', {{
        method:'POST', headers:{{'content-type':'application/json'}},
        body: JSON.stringify({{readings:[reading]}})
      }});
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();
      const p = data.failure_probability[0];
      const pct = (p * 100).toFixed(1);
      // Risk word + colour are a REDUNDANT cue on top of the number (never colour-alone).
      let word = t('riskLow'), col = '#2b8a3e';
      if (p >= 0.66) {{ word = t('riskHigh'); col = '#c92a2a'; }}
      else if (p >= 0.33) {{ word = t('riskModerate'); col = '#e8590c'; }}
      result.innerHTML =
        '<div class="meter-wrap"><div class="headline">' + esc(t('failureProb')) +
        ': <b>' + pct + '%</b> &nbsp;<span class="muted">(' + esc(word) + ' · ' +
        esc(t('modelV')) + data.model_version +
        (data.persisted ? ' · ' + esc(t('logged')) : '') + ')</span></div>' +
        '<div class="meter"><i style="width:' + pct + '%;background:' + col + '"></i></div></div>';
      statusEl.textContent = '';
      if (data.persisted) setTimeout(() => location.reload(), 700);
    }} catch (err) {{
      result.innerHTML = '<p class="muted">' +
        t('predictFailed').replace('{{msg}}', esc(err.message)) + '</p>';
      statusEl.textContent = '';
    }} finally {{ btn.disabled = false; }}
  }});

  // --- F8: bring-your-own-data upload -------------------------------------------
  const fileInput = document.getElementById('file');
  const upStatus = document.getElementById('upStatus');
  const mapArea = document.getElementById('mapArea');
  const uploadResult = document.getElementById('uploadResult');
  let currentFile = null;

  fileInput.addEventListener('change', async () => {{
    uploadResult.innerHTML = ''; mapArea.innerHTML = '';
    if (!fileInput.files.length) return;
    currentFile = fileInput.files[0];
    upStatus.textContent = t('parsing');
    try {{
      const fd = new FormData(); fd.append('file', currentFile);
      const resp = await fetch('/demo/upload', {{ method:'POST', body: fd }});
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || ('HTTP ' + resp.status));
      renderMapping(data);
      upStatus.textContent = data.n_rows + ' ' + t('rows') + ' · ' + data.n_signals_matched +
        ' ' + t('of') + ' ' + data.feature_columns.length + ' ' + t('signalsAutoMatched');
    }} catch (err) {{
      upStatus.textContent = '';
      mapArea.innerHTML = '<p class="err">' +
        t('uploadFailed').replace('{{msg}}', esc(err.message)) + '</p>';
    }}
  }});

  function renderMapping(data) {{
    const sig = (I18N[lang] || I18N.en).sig || {{}};
    let rows = '';
    for (const s of data.feature_columns) {{
      const chosen = data.suggested_mapping[s];
      let opts = '<option value="">' + esc(t('leaveEmpty')) + '</option>';
      for (const h of data.headers) opts += '<option value="' + esc(h) + '"' +
        (h === chosen ? ' selected' : '') + '>' + esc(h) + '</option>';
      const conf = chosen ? t('autoMatched') : t('noMatch');
      const label = esc(sig[s] || s) + ' <code>' + esc(s) + '</code>';
      rows += '<tr><td>' + label + '</td><td><select data-sig="' +
        esc(s) + '">' + opts + '</select></td><td class="conf">' + esc(conf) + '</td></tr>';
    }}
    mapArea.innerHTML =
      '<table class="map"><thead><tr><th>' + esc(t('expectedSignal')) + '</th><th>' +
      esc(t('yourColumn')) + '</th><th></th></tr></thead><tbody>' + rows + '</tbody></table>' +
      '<div class="actions"><button type="button" class="go" id="scoreBtn">' +
      esc(t('scoreBatch')) + '</button></div>';
    document.getElementById('scoreBtn').addEventListener('click', scoreBatch);
  }}

  async function scoreBatch() {{
    const mapping = {{}};
    for (const sel of mapArea.querySelectorAll('select'))
      mapping[sel.dataset.sig] = sel.value === '' ? null : sel.value;
    const btn = document.getElementById('scoreBtn');
    btn.disabled = true; upStatus.textContent = t('scoring');
    try {{
      const fd = new FormData();
      fd.append('file', currentFile);
      fd.append('mapping', JSON.stringify(mapping));
      const resp = await fetch('/demo/upload', {{ method:'POST', body: fd }});
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || ('HTTP ' + resp.status));
      renderBatchResult(data);
      upStatus.textContent = '';
    }} catch (err) {{
      upStatus.textContent = '';
      uploadResult.innerHTML = '<p class="err">' +
        t('scoringFailed').replace('{{msg}}', esc(err.message)) + '</p>';
    }} finally {{ btn.disabled = false; }}
  }}

  function renderBatchResult(data) {{
    const s = data.summary;
    const maxc = Math.max(1, ...s.histogram);
    let bars = '';
    for (const c of s.histogram)
      bars += '<div class="bar" title="' + c + '" style="height:' +
        (100 * c / maxc) + '%"></div>';
    const total = data.n_signals_provided + data.unmapped_signals.length;
    const N = Math.min(data.failure_probability.length, 50);
    let rows = '';
    for (let i = 0; i < N; i++)
      rows += '<tr><td>' + (i + 1) + '</td><td>' +
        (data.failure_probability[i] * 100).toFixed(1) + '%</td></tr>';
    const more = data.n_rows > N
      ? '<p class="muted">' + t('showingFirst').replace('{{n}}', N).replace('{{total}}', data.n_rows) + '</p>' : '';
    const missing = data.unmapped_signals.length
      ? '<p class="muted">' + t('missingEraNull').replace('{{list}}',
          data.unmapped_signals.map(x => '<code>' + esc(x) + '</code>').join(', ')) + '</p>' : '';
    uploadResult.innerHTML =
      '<div class="banner">' + t('batchBanner') + '</div>' +
      '<div class="summary">' +
        '<div class="stat"><b>' + data.n_rows + '</b><span>' + esc(t('rowsScored')) + '</span></div>' +
        '<div class="stat"><b>' + data.n_signals_provided + ' / ' + total +
          '</b><span>' + esc(t('signalsProvided')) + '</span></div>' +
        '<div class="stat"><b>' + s.pct_high_risk.toFixed(0) + '%</b><span>' +
          esc(t('atRisk').replace('{{t}}', (s.threshold * 100).toFixed(0)).replace('{{n}}', s.n_high_risk)) +
          '</span></div>' +
      '</div>' + missing +
      '<div class="hist">' + bars + '</div>' +
      '<div class="hist-axis"><span>0%</span><span>' + esc(t('axisProb')) +
        '</span><span>100%</span></div>' +
      '<table class="rows"><thead><tr><th>' + esc(t('colRow')) + '</th><th>' +
      esc(t('colProb')) + '</th></tr></thead><tbody>' + rows + '</tbody></table>' + more;
  }}

  // --- F14a: generate your own fleet --------------------------------------------
  // The API only ENQUEUES: POST returns 202 with a run id, the forge runs in a separate
  // worker service, and this polls until the run is terminal (ADR-026 / S2).
  const genForm = document.getElementById('genForm');
  const genStatus = document.getElementById('genStatus');
  const genResult = document.getElementById('genResult');
  const GEN_FIELDS = [
    {{ id:'genUnits', key:'genUnits', min:GEN_CAPS.min_units, max:GEN_CAPS.max_units,
       step:1, value:GEN_CAPS.default_units }},
    {{ id:'genDays', key:'genDays', min:GEN_CAPS.min_days, max:GEN_CAPS.max_days,
       step:1, value:GEN_CAPS.default_days }},
    {{ id:'genSeed', key:'genSeed', min:0, max:9999, step:1, value:GEN_CAPS.default_seed }},
  ];
  function buildGenFields() {{
    const host = document.getElementById('genFields');
    if (!host) return;
    const keep = {{}};
    for (const el of host.querySelectorAll('input')) keep[el.id] = el.value;
    host.innerHTML = GEN_FIELDS.map(f =>
      '<label class="field"><span class="lab">' + esc(t(f.key)) + '</span>' +
      '<input type="number" id="' + f.id + '" min="' + f.min + '" max="' + f.max +
      '" step="' + f.step + '" value="' + (keep[f.id] != null ? keep[f.id] : f.value) + '">' +
      '</label>').join('');
    document.getElementById('genCapHint').textContent =
      t('genCapHint').replace('{{cap}}', GEN_CAPS.max_unit_days);
    document.getElementById('genOff').hidden = GEN_ENABLED;
    genForm.querySelector('button').disabled = !GEN_ENABLED;
  }}

  genForm.addEventListener('submit', async (e) => {{
    e.preventDefault();
    const btn = genForm.querySelector('button');
    btn.disabled = true; genResult.innerHTML = ''; genStatus.textContent = '…';
    const body = {{
      n_units: Number(document.getElementById('genUnits').value),
      days: Number(document.getElementById('genDays').value),
      seed: Number(document.getElementById('genSeed').value),
    }};
    try {{
      const resp = await fetch('/demo/generate', {{
        method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(body) }});
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.status);
      genStatus.textContent = t('genQueued') + ' · ' + t('genWorker').replace('{{name}}', data.worker);
      await pollRun(data.run_id);
    }} catch (err) {{
      genStatus.textContent = '';
      genResult.innerHTML = '<p class="err">' +
        t('genFailed').replace('{{msg}}', esc(err.message)) + '</p>';
    }} finally {{ btn.disabled = !GEN_ENABLED ? true : false; }}
  }});

  async function pollRun(runId) {{
    // The worker is a cold-starting container; poll patiently rather than hammering it.
    for (let i = 0; i < 120; i++) {{
      await new Promise(r => setTimeout(r, 1500));
      const resp = await fetch('/demo/generate/' + encodeURIComponent(runId));
      const run = await resp.json();
      if (!resp.ok) throw new Error(run.detail || resp.status);
      if (run.status === 'running') genStatus.textContent = t('genRunning');
      if (run.status === 'failed') throw new Error(run.error || 'failed');
      if (run.status === 'succeeded') {{
        genStatus.textContent = '';
        return renderFleet(runId, run);
      }}
    }}
    throw new Error('timed out');
  }}

  async function renderFleet(runId, run) {{
    const [report, page] = await Promise.all([
      fetch('/demo/generate/' + encodeURIComponent(runId) + '/report').then(r => r.json()),
      fetch('/demo/generate/' + encodeURIComponent(runId) + '/rows?limit=20').then(r => r.json()),
    ]);
    const pct = (x) => (x * 100).toFixed(1) + '%';
    const fleetRows = report.units.map(u =>
      '<tr class="' + (u.flagged ? 'flag' : '') + '"><td><code>' + esc(u.unit_id) + '</code></td>' +
      '<td><div class="bar-cell"><div class="track"><i style="width:' +
        (u.risk * 100).toFixed(0) + '%"></i></div><span>' + pct(u.risk) + '</span></div></td>' +
      '<td class="muted">' + pct(u.peak) + '</td>' +
      '<td class="muted">' + pct(u.high_risk_share) + '</td>' +
      '<td><span class="pill ' + (u.flagged ? 'on' : '') + '">' +
        esc(u.flagged ? t('genFlag') : t('genOk')) + '</span></td></tr>').join('');

    const cols = page.columns;
    const head = cols.map(c => '<th>' + esc(c) + '</th>').join('');
    const dataRows = page.rows.map(r => '<tr>' + cols.map(c => {{
      const v = r[c];
      if (v === null || v === undefined) return '<td class="muted">null</td>';   // era-NULL
      return '<td>' + esc(typeof v === 'number' ? v.toFixed(1) : v) + '</td>';
    }}).join('') + '</tr>').join('');

    genResult.innerHTML =
      (report.demo ? '<div class="banner">' + t('genDemo') + '</div>' : '') +
      '<h2>' + esc(t('genReportTitle')) + '</h2>' +
      '<div class="summary">' +
        '<div class="stat"><b>' + report.n_flagged + ' / ' + report.n_units +
          '</b><span>' + esc(t('genFlagged')) + '</span></div>' +
        '<div class="stat"><b>' + run.n_rows.toLocaleString() + '</b><span>' +
          esc(t('genRowsGen')) + '</span></div>' +
        '<div class="stat"><b>v' + esc(report.model_version) + '</b><span>' +
          esc(t('colModel')) + '</span></div>' +
      '</div>' +
      '<p class="conf">' + t('genRule').replace('{{t}}',
        (report.flag_threshold * 100).toFixed(0)) + '</p>' +
      '<table class="fleet"><thead><tr><th>' + esc(t('genColUnit')) + '</th><th>' +
        esc(t('genColRisk')) + '</th><th>' + esc(t('genColPeak')) + '</th><th>' +
        esc(t('genColShare')) + '</th><th></th></tr></thead><tbody>' + fleetRows +
      '</tbody></table>' +
      '<h2>' + esc(t('genBrowseTitle')) + '</h2>' +
      '<p class="muted">' + esc(t('genShowing').replace('{{n}}', page.rows.length)
        .replace('{{total}}', page.total_rows.toLocaleString())) + '</p>' +
      '<div style="overflow-x:auto"><table class="rows"><thead><tr>' + head +
      '</tr></thead><tbody>' + dataRows + '</tbody></table></div>';
  }}
  </script>
</main></body></html>"""
