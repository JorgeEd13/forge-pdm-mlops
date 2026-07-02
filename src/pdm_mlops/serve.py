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

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from . import config, features
from . import registry as _registry

# FastAPI/pydantic are the [serve] extra — imported at module load so the app object
# exists for `pdm serve` and the tests, but kept out of the core dependency set.
from fastapi import FastAPI, HTTPException
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


class ModelInfo(BaseModel):
    """The live production model's identity — for auditable serving."""

    registered_model: str
    production_version: str
    primary_metric: str
    metric_value: float


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


def create_app(store: ModelStore | None = None) -> FastAPI:
    """Build the serving app, optionally with an injected :class:`ModelStore`.

    Tests inject a store bound to a tmp registry; ``pdm serve`` uses the default
    (the local SQLite backend). The model is loaded lazily on first use, so the app
    starts cleanly even with nothing promoted yet.
    """
    store = store or ModelStore()
    app = FastAPI(
        title="forge-pdm-mlops serving",
        description="Serves the production-aliased failure classifier (F4).",
        version=config_version(),
    )
    app.state.store = store

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
        return ModelInfo(
            registered_model=config.REGISTERED_MODEL_NAME,
            production_version=loaded.version,
            primary_metric=config.PRIMARY_METRIC,
            metric_value=metric,
        )

    @app.post("/predict", response_model=PredictResponse)
    def predict(request: PredictRequest) -> PredictResponse:
        """Score a batch of readings → per-row failure probability."""
        try:
            loaded = store.load()
        except LookupError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        X = _to_frame(request.readings)
        proba = loaded.predict_proba(X)
        return PredictResponse(
            model_version=loaded.version,
            n_rows=len(X),
            failure_probability=[float(p) for p in proba],
        )

    return app


def config_version() -> str:
    """The package version, for the OpenAPI doc (kept out of the import cycle)."""
    from . import __version__

    return __version__
