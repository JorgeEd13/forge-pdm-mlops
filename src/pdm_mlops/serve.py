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
from . import registry as _registry
from . import store_pg
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
    """
    store = store or ModelStore()
    log = prediction_log if prediction_log is not None else store_pg.open_log()
    app = FastAPI(
        title="forge-pdm-mlops serving",
        description="Serves the production-aliased failure classifier (F4).",
        version=config_version(),
    )
    app.state.store = store
    app.state.prediction_log = log

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
                "with a 'bring your own data' CSV/Parquet batch upload",
                "GET /health": "liveness + whether a model is loaded",
                "GET /model-info": "the live production version + the metric it was gated on",
                "POST /predict": "score a batch of J1939 readings → per-row failure probability",
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

    @app.get("/demo", response_class=HTMLResponse)
    def demo() -> HTMLResponse:
        """A self-contained 'set parameters → get a prediction' page (F7).

        No CDN, no external asset — inline CSS/JS only (the clean-room/offline
        discipline). Shows the same ``demo=fixture`` honesty banner the rest of the
        surface carries, a form seeded with the feature signal names, and the recent
        predictions read back from the managed DB (empty when none is configured).
        """
        recent = log.recent(limit=10) if log is not None else []
        return HTMLResponse(_render_demo_page(recent, persistence=log is not None))

    return app


def config_version() -> str:
    """The package version, for the OpenAPI doc (kept out of the import cycle)."""
    from . import __version__

    return __version__


# --- the demo page ------------------------------------------------------------

# A neutral, example set of J1939 signal values to seed the form so a first-time
# visitor can hit "Predict" without knowing the schema. Plausible healthy-ish readings;
# NOT a fixture row (the demo is about the wiring, not a reported number).
_DEMO_SEED: dict[str, float] = {
    "engine_speed_rpm": 1800,
    "coolant_temp_c": 92,
    "oil_pressure_kpa": 320,
    "engine_load_pct": 65,
    "fuel_rate_lph": 24,
    "boost_pressure_kpa": 150,
    "egt_c": 480,
    "def_level_pct": 55,
    "vibration_mms": 3.2,
}


def _render_demo_page(
    recent: list[store_pg.LoggedPrediction], *, persistence: bool
) -> str:
    """Render the self-contained demo HTML (inline CSS/JS, no external asset).

    A form seeded with the :data:`features.FEATURE_COLUMNS` signals → a single failure
    **probability** rendered as a labelled meter (the number carries the meaning; colour
    is a redundant cue, never the only one). The ``demo=fixture`` honesty banner is shown
    inline, matching ``/model-info`` and the README. The recent-predictions panel reads
    from the managed DB (Neon Postgres, F7) when configured, else a short "logging off" note.
    """
    import html

    # Form inputs, one per signal, seeded with a neutral example value.
    fields = "\n".join(
        f'<label class="field"><span>{html.escape(name)}</span>'
        f'<input type="number" step="any" name="{html.escape(name)}" '
        f'value="{_DEMO_SEED.get(name, "")}"></label>'
        for name in features.FEATURE_COLUMNS
    )

    # Recent predictions table (server-rendered), or the logging-off note.
    if persistence:
        if recent:
            rows = "\n".join(
                f"<tr><td>{html.escape(r.created_at.strftime('%Y-%m-%d %H:%M:%S'))} UTC</td>"
                f"<td>v{html.escape(r.model_version)}</td>"
                f"<td>{r.failure_probability:.3f}</td></tr>"
                for r in recent
            )
            recent_html = (
                "<table class='recent'><thead><tr><th>when</th><th>model</th>"
                f"<th>failure prob.</th></tr></thead><tbody>{rows}</tbody></table>"
            )
        else:
            recent_html = "<p class='muted'>No predictions logged yet — submit one above.</p>"
        recent_note = "Logged to a managed <strong>Postgres</strong> instance (Neon)."
    else:
        recent_html = ""
        recent_note = (
            "Prediction logging is <strong>off</strong> (no <code>DATABASE_URL</code>) — "
            "the managed-DB panel appears on the Cloud Run deploy."
        )

    return _DEMO_TEMPLATE.format(
        fields=fields,
        recent_html=recent_html,
        recent_note=recent_note,
    )


# Inline CSS/JS only (clean-room / offline / Artifact-CSP-safe by the same discipline).
# The JS posts the form to /demo/predict and renders the returned probability as a
# labelled meter; the number and the risk word are the primary encoding, the bar colour
# a redundant cue (accessibility: never colour-alone).
_DEMO_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>forge-pdm-mlops — try a prediction</title>
<style>
  :root {{ --ink:#1a2233; --muted:#5b6472; --line:#e2e6ec; --surface:#f7f8fa;
           --accent:#3b5bdb; --demo:#8a5a00; --demo-bg:#fff6e0; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
          color:var(--ink); background:#fff; }}
  main {{ max-width:820px; margin:0 auto; padding:24px 20px 64px; }}
  h1 {{ font-size:1.5rem; margin:.2rem 0 .1rem; }}
  .sub {{ color:var(--muted); margin:0 0 20px; }}
  .banner {{ background:var(--demo-bg); color:var(--demo); border:1px solid #f0d9a8;
             border-radius:8px; padding:10px 14px; font-size:.9rem; margin-bottom:22px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(160px,1fr));
           gap:12px; }}
  .field {{ display:flex; flex-direction:column; gap:4px; font-size:.82rem;
            color:var(--muted); }}
  .field input {{ font-size:.95rem; padding:7px 9px; border:1px solid var(--line);
                  border-radius:6px; color:var(--ink); }}
  .actions {{ margin:20px 0; display:flex; gap:12px; align-items:center; }}
  button {{ background:var(--accent); color:#fff; border:0; border-radius:8px;
            padding:10px 20px; font-size:.95rem; font-weight:600; cursor:pointer; }}
  button:disabled {{ opacity:.5; cursor:default; }}
  #result {{ margin-top:8px; }}
  .meter-wrap {{ display:flex; flex-direction:column; gap:6px; max-width:520px; }}
  .meter {{ height:14px; background:var(--surface); border:1px solid var(--line);
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
  .drop input[type=file] {{ font-size:.9rem; }}
  table.map {{ width:100%; border-collapse:collapse; font-size:.86rem; margin-top:14px; }}
  table.map th, table.map td {{ text-align:left; padding:6px 8px;
    border-bottom:1px solid var(--line); vertical-align:middle; }}
  table.map th {{ color:var(--muted); font-weight:600; }}
  table.map select {{ font-size:.86rem; padding:5px 7px; border:1px solid var(--line);
    border-radius:6px; color:var(--ink); max-width:100%; }}
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
  .err {{ color:#c92a2a; }}
</style></head>
<body><main>
  <h1>forge-pdm-mlops — try a prediction</h1>
  <p class="sub">Set the J1939 signal values, get the model's failure probability.</p>
  <div class="banner"><strong>DEMO model.</strong> The served model is trained on a small
    committed <em>smoke fixture</em> — its probabilities illustrate the wired endpoint,
    they are <strong>not</strong> a reported result. The real ≈0.82 ROC-AUC model is trained
    on the full dataset locally (see <a href="/model-info">/model-info</a>). </div>

  <form id="f">
    <div class="grid">{fields}</div>
    <div class="actions">
      <button type="submit">Predict</button>
      <span class="muted" id="status"></span>
    </div>
  </form>
  <div id="result"></div>

  <section>
    <h2>Bring your own data</h2>
    <p class="muted">Upload a CAN/J1939 batch (<code>.csv</code> or <code>.parquet</code>) to
      score every row. Your column names don't have to match ours — you'll map them in the
      next step, and any signal you leave unmapped is treated as a missing sensor (era-NULL),
      so a partial dataset still scores. Same <strong>DEMO</strong> model as above; nothing
      you upload is stored.</p>
    <div class="drop">
      <input type="file" id="file" accept=".csv,.parquet">
      <span class="muted" id="upStatus"></span>
    </div>
    <div id="mapArea"></div>
    <div id="uploadResult"></div>
  </section>

  <section>
    <h2>Recent predictions</h2>
    <p class="muted">{recent_note}</p>
    {recent_html}
  </section>

  <script>
  const form = document.getElementById('f');
  const result = document.getElementById('result');
  const statusEl = document.getElementById('status');
  form.addEventListener('submit', async (e) => {{
    e.preventDefault();
    const btn = form.querySelector('button');
    btn.disabled = true; statusEl.textContent = 'scoring…';
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
      let word = 'low', col = '#2b8a3e';
      if (p >= 0.66) {{ word = 'high'; col = '#c92a2a'; }}
      else if (p >= 0.33) {{ word = 'moderate'; col = '#e8590c'; }}
      result.innerHTML =
        '<div class="meter-wrap"><div class="headline">Failure probability: <b>' +
        pct + '%</b> &nbsp;<span class="muted">(' + word + ' risk · model v' +
        data.model_version + (data.persisted ? ' · logged' : '') + ')</span></div>' +
        '<div class="meter"><i style="width:' + pct + '%;background:' + col + '"></i></div></div>';
      statusEl.textContent = '';
      if (data.persisted) setTimeout(() => location.reload(), 700);
    }} catch (err) {{
      result.innerHTML = '<p class="muted">Prediction failed: ' + err.message +
        ' (is a model promoted? see <a href="/health">/health</a>)</p>';
      statusEl.textContent = '';
    }} finally {{ btn.disabled = false; }}
  }});

  // --- F8: bring-your-own-data upload -----------------------------------------
  const fileInput = document.getElementById('file');
  const upStatus = document.getElementById('upStatus');
  const mapArea = document.getElementById('mapArea');
  const uploadResult = document.getElementById('uploadResult');
  let currentFile = null;

  const esc = (s) => String(s).replace(/[&<>"]/g, c =>
    ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}})[c]);

  fileInput.addEventListener('change', async () => {{
    uploadResult.innerHTML = ''; mapArea.innerHTML = '';
    if (!fileInput.files.length) return;
    currentFile = fileInput.files[0];
    upStatus.textContent = 'parsing…';
    try {{
      const fd = new FormData(); fd.append('file', currentFile);
      const resp = await fetch('/demo/upload', {{ method:'POST', body: fd }});
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || ('HTTP ' + resp.status));
      renderMapping(data);
      upStatus.textContent = data.n_rows + ' rows · ' + data.n_signals_matched +
        ' of ' + data.feature_columns.length + ' signals auto-matched';
    }} catch (err) {{
      upStatus.textContent = '';
      mapArea.innerHTML = '<p class="err">Upload failed: ' + esc(err.message) + '</p>';
    }}
  }});

  function renderMapping(data) {{
    let rows = '';
    for (const sig of data.feature_columns) {{
      const chosen = data.suggested_mapping[sig];
      let opts = '<option value="">— (leave empty · era-NULL) —</option>';
      for (const h of data.headers) opts += '<option value="' + esc(h) + '"' +
        (h === chosen ? ' selected' : '') + '>' + esc(h) + '</option>';
      const conf = chosen ? 'auto-matched' : 'no match — map or leave empty';
      rows += '<tr><td><code>' + esc(sig) + '</code></td><td><select data-sig="' +
        esc(sig) + '">' + opts + '</select></td><td class="conf">' + conf + '</td></tr>';
    }}
    mapArea.innerHTML =
      '<table class="map"><thead><tr><th>expected signal</th><th>your column</th>' +
      '<th></th></tr></thead><tbody>' + rows + '</tbody></table>' +
      '<div class="actions"><button type="button" id="scoreBtn">Score batch</button></div>';
    document.getElementById('scoreBtn').addEventListener('click', scoreBatch);
  }}

  async function scoreBatch() {{
    const mapping = {{}};
    for (const sel of mapArea.querySelectorAll('select'))
      mapping[sel.dataset.sig] = sel.value === '' ? null : sel.value;
    const btn = document.getElementById('scoreBtn');
    btn.disabled = true; upStatus.textContent = 'scoring…';
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
      uploadResult.innerHTML = '<p class="err">Scoring failed: ' + esc(err.message) + '</p>';
    }} finally {{ btn.disabled = false; }}
  }}

  function renderBatchResult(data) {{
    const s = data.summary;
    const maxc = Math.max(1, ...s.histogram);
    let bars = '';
    for (const c of s.histogram)
      bars += '<div class="bar" title="' + c + ' rows" style="height:' +
        (100 * c / maxc) + '%"></div>';
    const total = data.n_signals_provided + data.unmapped_signals.length;
    const N = Math.min(data.failure_probability.length, 50);
    let rows = '';
    for (let i = 0; i < N; i++)
      rows += '<tr><td>' + (i + 1) + '</td><td>' +
        (data.failure_probability[i] * 100).toFixed(1) + '%</td></tr>';
    const more = data.n_rows > N
      ? '<p class="muted">Showing the first ' + N + ' of ' + data.n_rows + ' rows.</p>' : '';
    const missing = data.unmapped_signals.length
      ? '<p class="muted">Missing signals scored as era-NULL: ' +
        data.unmapped_signals.map(x => '<code>' + esc(x) + '</code>').join(', ') + '</p>' : '';
    uploadResult.innerHTML =
      '<div class="banner"><strong>DEMO model.</strong> Your batch was scored by the ' +
      'fixture-trained demo model (see <a href="/model-info">/model-info</a>) — illustrative, ' +
      'not a reported result. Nothing you uploaded was stored.</div>' +
      '<div class="summary">' +
        '<div class="stat"><b>' + data.n_rows + '</b><span>rows scored</span></div>' +
        '<div class="stat"><b>' + data.n_signals_provided + ' / ' + total +
          '</b><span>signals provided</span></div>' +
        '<div class="stat"><b>' + s.pct_high_risk.toFixed(0) + '%</b><span>≥ ' +
          (s.threshold * 100).toFixed(0) + '% risk (' + s.n_high_risk + ' rows)</span></div>' +
      '</div>' + missing +
      '<div class="hist">' + bars + '</div>' +
      '<div class="hist-axis"><span>0%</span><span>failure probability →</span><span>100%</span></div>' +
      '<table class="rows"><thead><tr><th>row</th><th>failure prob.</th></tr></thead><tbody>' +
      rows + '</tbody></table>' + more;
  }}
  </script>
</main></body></html>"""
