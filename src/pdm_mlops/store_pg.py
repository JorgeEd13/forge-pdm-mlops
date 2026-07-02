"""Prediction log on a **managed** SQL database — the state behind the demo (F7).

The serving layer (F4) is deliberately stateless: it resolves the ``production`` model
and answers a probability. That is the right shape for a model endpoint, but it gives a
*managed database* nothing honest to do — and the F7 gate is exactly "operate a managed
cloud **resource** in production", not just a managed container. This module is that
resource's job: every prediction the **demo UI** serves is appended here, and the page
reads the most recent ones back. On Cloud Run the URL points at **Cloud SQL for
Postgres**; the same code runs against a local SQLite file in tests.

**Graceful degrade is a hard requirement.** The database is a Cloud-Run-only
enhancement. When ``DATABASE_URL`` is unset — a local ``pdm serve``, the Hugging Face
Space (F6), CI — :func:`open_log` returns ``None`` and the demo simply doesn't persist:
the prediction still returns, the "recent predictions" panel is just empty. So adding the
managed resource **cannot break** any of the existing deploy targets, and the offline
tests never need a server.

**No PII, by construction.** The only things stored are the submitted J1939 signal values
(synthetic sensor floats), the returned failure probability, the model version that
produced it, and a UTC timestamp. There is no user identity anywhere in the schema — the
demo is anonymous and the inputs are physics, not people.

The driver (``sqlalchemy`` + ``psycopg``) is the ``[cloud]`` extra, imported lazily so the
package, core CI, and every non-cloud deploy import ``pdm_mlops`` without it — the same
extra-gated discipline as ``[serve]`` / ``[ops]`` (ADR-005/009/013).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import features

#: The env var Cloud Run injects (from Secret Manager) with the Cloud SQL connection
#: string, e.g. ``postgresql+psycopg://user:pass@/db?host=/cloudsql/<instance>``. Unset
#: everywhere else — that is the signal to run without persistence (graceful degrade).
DATABASE_URL_ENV = "DATABASE_URL"

#: One row per served demo prediction. Feature values are stored as a JSON object keyed
#: by :data:`features.FEATURE_COLUMNS` (era-NULL as JSON ``null``) so the schema doesn't
#: churn if the signal set changes — the model contract already owns that ordering.
_TABLE = "demo_predictions"


@dataclass(frozen=True)
class LoggedPrediction:
    """A single stored prediction, as the demo page renders it (newest first)."""

    created_at: datetime
    model_version: str
    failure_probability: float
    readings: dict[str, float | None]


class PredictionLog:
    """A thin append/read layer over the ``demo_predictions`` table.

    Holds a single SQLAlchemy :class:`Engine` (a pooled connection to the managed DB) and
    ensures the table exists on construction. Kept as a small object — not module globals —
    so a test can point it at a tmp SQLite file and :func:`create_app` can inject one, the
    same pattern as :class:`serve.ModelStore`.
    """

    def __init__(self, url: str) -> None:
        # Imported here (not at module load) so importing pdm_mlops never needs the
        # [cloud] extra — only actually opening a log does.
        import sqlalchemy as sa

        self._sa = sa
        self._engine = sa.create_engine(url, pool_pre_ping=True)
        self._table = sa.Table(
            _TABLE,
            sa.MetaData(),
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("model_version", sa.String(64), nullable=False),
            sa.Column("failure_probability", sa.Float, nullable=False),
            # JSON of the submitted signals. sa.JSON maps to Postgres JSONB-compatible
            # JSON and to a TEXT column on SQLite — both round-trip the dict.
            sa.Column("readings", sa.JSON, nullable=False),
        )
        self._table.metadata.create_all(self._engine)

    def log(
        self,
        *,
        model_version: str,
        failure_probability: float,
        readings: dict[str, float | None],
    ) -> None:
        """Append one served prediction. Never raises into the request path.

        A logging failure (the managed DB briefly unreachable, say) must not turn a
        successful prediction into a 500 — the model already answered. So a store error is
        swallowed to a no-op here; the endpoint returns the probability regardless, and the
        only cost is a missing row in the recent-predictions panel.
        """
        row = {
            "created_at": datetime.now(timezone.utc),
            "model_version": str(model_version),
            "failure_probability": float(failure_probability),
            # Restrict to the known signal keys so a crafted request can't widen the row.
            "readings": _clean_readings(readings),
        }
        try:
            with self._engine.begin() as conn:
                conn.execute(self._sa.insert(self._table).values(**row))
        except Exception:  # noqa: BLE001 — logging is best-effort, see docstring
            return

    def recent(self, limit: int = 10) -> list[LoggedPrediction]:
        """The most recent stored predictions, newest first (for the demo panel)."""
        stmt = (
            self._sa.select(self._table)
            .order_by(self._table.c.id.desc())
            .limit(int(limit))
        )
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(stmt).mappings().all()
        except Exception:  # noqa: BLE001 — a read failure shows an empty panel, not a 500
            return []
        return [
            LoggedPrediction(
                created_at=_as_utc(r["created_at"]),
                model_version=r["model_version"],
                failure_probability=float(r["failure_probability"]),
                readings=_as_dict(r["readings"]),
            )
            for r in rows
        ]

    def dispose(self) -> None:
        """Close the connection pool (tests; process shutdown)."""
        self._engine.dispose()


def open_log(url: str | None = None) -> PredictionLog | None:
    """Open the prediction log, or ``None`` when no database is configured.

    ``url`` defaults to the :data:`DATABASE_URL_ENV` env var. **Returning ``None`` is the
    normal, supported path** for every non-cloud target (local, HF Space, CI): the demo
    then runs without persistence. A malformed URL or an unreachable DB also degrades to
    ``None`` rather than crashing app start — the endpoint's value is the model, not the
    log.
    """
    resolved = url if url is not None else os.environ.get(DATABASE_URL_ENV)
    if not resolved:
        return None
    try:
        return PredictionLog(resolved)
    except Exception:  # noqa: BLE001 — never let the log break app startup
        return None


def _clean_readings(readings: dict[str, float | None]) -> dict[str, float | None]:
    """Keep only the known signal keys, coerced to float/None (no PII, no surprises)."""
    cleaned: dict[str, float | None] = {}
    for key in features.FEATURE_COLUMNS:
        if key in readings and readings[key] is not None:
            cleaned[key] = float(readings[key])
        else:
            cleaned[key] = None
    return cleaned


def _as_dict(value: Any) -> dict[str, float | None]:
    """Normalise a stored ``readings`` cell to a dict (SQLite may hand back a JSON str)."""
    if isinstance(value, str):
        return json.loads(value)
    return dict(value) if value is not None else {}


def _as_utc(value: datetime) -> datetime:
    """Ensure a timezone-aware UTC datetime (SQLite loses the tz on the round-trip)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
