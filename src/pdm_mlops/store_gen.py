"""The generated-fleet store — the state the web/worker split talks through (F14a).

The API and the generation worker are **separate deployable units** (ADR-026 / decision
S2): they never call each other in-process, so the managed Postgres is the whole of their
shared state. This module is that contract, in three tables:

* ``generation_runs`` — one row per request. The API inserts it ``queued`` and returns
  immediately; the worker moves it ``running`` → ``succeeded`` / ``failed``. This row *is*
  the polling surface, and the reason a kicked-off run survives the API container scaling
  to zero.
* ``generation_readings`` — the generated fleet, capped. Signals live in a **JSON object**
  keyed by signal name — the same schema-stable shape :mod:`store_pg` uses, so a change to
  the signal set (F10 adds failure modes and their signature channels) does not need a
  migration and cannot silently misalign already-stored rows.
* ``generation_units`` — the per-vehicle roll-up, cached **per model version**. Scoring
  tens of thousands of rows on every page view would be the one path in this demo that
  could actually approach the Cloud Run request timeout; caching it keyed by the promoted
  version keeps the report fast *without* breaking the property the whole registry exists
  for — promote or roll back a model and the report is recomputed, because the key changed.

**Retention is not optional.** Neon's free tier is 0.5 GB and this demo is public: without
a cap the store grows until the tier fails, which would take the *prediction log* down with
it. :meth:`GenerationStore.prune` evicts the oldest finished runs down to a row budget.

**Errors are NOT swallowed here** — deliberately unlike :class:`store_pg.PredictionLog`,
whose logging is best-effort because the model already answered. Here a write failure means
the user's run is lost, so it must surface: the worker marks the run ``failed`` with the
message, and the poll shows it. What *is* graceful is the absence of a database entirely
(:func:`open_store` → ``None``), which simply makes the feature unavailable — the rest of
the demo is untouched (a local ``pdm serve``, the HF Space, CI).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd

from . import features, generate
from .store_pg import DATABASE_URL_ENV  # one env var for the one managed database

_RUNS = "generation_runs"
_READINGS = "generation_readings"
_UNITS = "generation_units"

#: Run lifecycle. ``queued`` is written by the API, the rest by the worker.
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_FAILED)


@dataclass(frozen=True)
class GenerationRun:
    """A generation request and where it got to — the polling payload."""

    run_id: str
    status: str
    n_units: int
    days: int
    seed: int
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    n_rows: int = 0
    error: str | None = None

    @property
    def spec(self) -> generate.GenerationSpec:
        return generate.GenerationSpec(
            n_units=self.n_units, days=self.days, seed=self.seed
        )

    @property
    def done(self) -> bool:
        return self.status in TERMINAL_STATUSES


class GenerationStore:
    """Runs + generated readings + the cached roll-up, over one SQLAlchemy engine.

    The same code runs against the managed Postgres (Neon, behind Cloud Run) and against a
    tmp SQLite file in tests — the pattern :class:`store_pg.PredictionLog` already
    established. Held as an object, not module globals, so tests and :func:`serve.create_app`
    can inject one.
    """

    def __init__(self, url: str) -> None:
        # Lazy, like store_pg: importing pdm_mlops must never require the [cloud] extra.
        import sqlalchemy as sa

        self._sa = sa
        self._engine = sa.create_engine(url, pool_pre_ping=True)
        meta = sa.MetaData()
        self._runs = sa.Table(
            _RUNS,
            meta,
            sa.Column("run_id", sa.String(36), primary_key=True),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("n_units", sa.Integer, nullable=False),
            sa.Column("days", sa.Integer, nullable=False),
            sa.Column("seed", sa.Integer, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True)),
            sa.Column("finished_at", sa.DateTime(timezone=True)),
            sa.Column("n_rows", sa.Integer, nullable=False, default=0),
            sa.Column("error", sa.Text),
        )
        self._readings = sa.Table(
            _READINGS,
            meta,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.String(36), nullable=False, index=True),
            sa.Column("unit_id", sa.String(48), nullable=False),
            sa.Column("t_index", sa.Integer, nullable=False),
            # Signal values keyed by name (era-NULL as JSON null) — schema-stable.
            sa.Column("readings", sa.JSON, nullable=False),
        )
        self._units = sa.Table(
            _UNITS,
            meta,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.String(36), nullable=False, index=True),
            # The roll-up is only valid for the model that produced it (see module docstring).
            sa.Column("model_version", sa.String(64), nullable=False),
            sa.Column("unit_id", sa.String(48), nullable=False),
            sa.Column("n_rows", sa.Integer, nullable=False),
            sa.Column("risk", sa.Float, nullable=False),
            sa.Column("peak", sa.Float, nullable=False),
            sa.Column("high_risk_share", sa.Float, nullable=False),
            sa.Column("flagged", sa.Boolean, nullable=False),
        )
        meta.create_all(self._engine)

    # --- runs -----------------------------------------------------------------

    def create_run(self, spec: generate.GenerationSpec) -> GenerationRun:
        """Insert a ``queued`` run and return it. The API does this, then triggers the job."""
        run = GenerationRun(
            run_id=str(uuid.uuid4()),
            status=STATUS_QUEUED,
            n_units=spec.n_units,
            days=spec.days,
            seed=spec.seed,
            created_at=datetime.now(timezone.utc),
        )
        with self._engine.begin() as conn:
            conn.execute(
                self._sa.insert(self._runs).values(
                    run_id=run.run_id,
                    status=run.status,
                    n_units=run.n_units,
                    days=run.days,
                    seed=run.seed,
                    created_at=run.created_at,
                    n_rows=0,
                )
            )
        return run

    def get_run(self, run_id: str) -> GenerationRun | None:
        stmt = self._sa.select(self._runs).where(self._runs.c.run_id == run_id)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if row is None:
            return None
        return GenerationRun(
            run_id=row["run_id"],
            status=row["status"],
            n_units=int(row["n_units"]),
            days=int(row["days"]),
            seed=int(row["seed"]),
            created_at=_as_utc(row["created_at"]),
            started_at=_as_utc(row["started_at"]) if row["started_at"] else None,
            finished_at=_as_utc(row["finished_at"]) if row["finished_at"] else None,
            n_rows=int(row["n_rows"] or 0),
            error=row["error"],
        )

    def mark_running(self, run_id: str) -> None:
        self._update_run(run_id, status=STATUS_RUNNING, started_at=datetime.now(timezone.utc))

    def mark_succeeded(self, run_id: str, *, n_rows: int) -> None:
        self._update_run(
            run_id,
            status=STATUS_SUCCEEDED,
            finished_at=datetime.now(timezone.utc),
            n_rows=int(n_rows),
        )

    def mark_failed(self, run_id: str, *, error: str) -> None:
        """Record a failure *on the run row* — a worker crash must be visible to the poller.

        The message is truncated: it is rendered back to an anonymous user, and an
        unbounded exception string is neither useful there nor safe to store unbounded.
        """
        self._update_run(
            run_id,
            status=STATUS_FAILED,
            finished_at=datetime.now(timezone.utc),
            error=str(error)[:500],
        )

    def _update_run(self, run_id: str, **values: Any) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                self._sa.update(self._runs)
                .where(self._runs.c.run_id == run_id)
                .values(**values)
            )

    # --- readings -------------------------------------------------------------

    def append_readings(self, run_id: str, readings: pd.DataFrame, *, chunk: int = 2_000) -> int:
        """Store the generated readings (signals only — no labels, see ``stored_columns``).

        Inserted in chunks so a capped run never builds one giant statement in the worker's
        memory. Returns the number of rows written.
        """
        missing = [c for c in generate.stored_columns() if c not in readings.columns]
        if missing:
            raise KeyError(f"generated readings missing columns {missing}")

        signals = list(features.FEATURE_COLUMNS)
        rows = [
            {
                "run_id": run_id,
                "unit_id": str(r.unit_id),
                "t_index": int(r.t_index),
                "readings": {s: _as_float_or_none(getattr(r, s)) for s in signals},
            }
            for r in readings.itertuples(index=False)
        ]
        with self._engine.begin() as conn:
            for start in range(0, len(rows), chunk):
                conn.execute(self._sa.insert(self._readings), rows[start : start + chunk])
        return len(rows)

    def readings(self, run_id: str, *, offset: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        """A page of stored readings, in time order per vehicle (the browse surface)."""
        stmt = (
            self._sa.select(self._readings)
            .where(self._readings.c.run_id == run_id)
            .order_by(self._readings.c.unit_id, self._readings.c.t_index)
            .offset(int(offset))
            .limit(int(limit))
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [
            {"unit_id": r["unit_id"], "t_index": int(r["t_index"]), **_as_dict(r["readings"])}
            for r in rows
        ]

    def readings_frame(self, run_id: str) -> pd.DataFrame:
        """**All** of a run's readings as a scorable frame (``unit_id``, ``t_index``, signals).

        Bounded by the caps (:data:`generate.MAX_UNIT_DAYS`), which is exactly why the
        report can afford to score the whole run in one request.
        """
        stmt = (
            self._sa.select(self._readings)
            .where(self._readings.c.run_id == run_id)
            .order_by(self._readings.c.unit_id, self._readings.c.t_index)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        if not rows:
            return pd.DataFrame(columns=generate.stored_columns())
        frame = pd.DataFrame(
            [
                {"unit_id": r["unit_id"], "t_index": int(r["t_index"]), **_as_dict(r["readings"])}
                for r in rows
            ]
        )
        # Re-impose the canonical column order; a signal absent from an older run's JSON
        # comes back as an era-NULL, which is a legitimate input (ADR-003), not an error.
        for signal in features.FEATURE_COLUMNS:
            if signal not in frame.columns:
                frame[signal] = None
        return frame.loc[:, generate.stored_columns()]

    def count_readings(self, run_id: str) -> int:
        stmt = (
            self._sa.select(self._sa.func.count())
            .select_from(self._readings)
            .where(self._readings.c.run_id == run_id)
        )
        with self._engine.connect() as conn:
            return int(conn.execute(stmt).scalar_one())

    # --- the cached per-vehicle roll-up --------------------------------------

    def save_report(self, run_id: str, model_version: str, units: Iterable[generate.UnitRisk]) -> None:
        rows = [
            {
                "run_id": run_id,
                "model_version": str(model_version),
                "unit_id": u.unit_id,
                "n_rows": u.n_rows,
                "risk": u.risk,
                "peak": u.peak,
                "high_risk_share": u.high_risk_share,
                "flagged": bool(u.flagged),
            }
            for u in units
        ]
        if not rows:
            return
        with self._engine.begin() as conn:
            # Replace any roll-up this run already has for this model version, so a retry
            # (or two racing report requests) can't double the vehicle list.
            conn.execute(
                self._sa.delete(self._units).where(
                    self._sa.and_(
                        self._units.c.run_id == run_id,
                        self._units.c.model_version == str(model_version),
                    )
                )
            )
            conn.execute(self._sa.insert(self._units), rows)

    def load_report(self, run_id: str, model_version: str) -> list[generate.UnitRisk]:
        """The cached roll-up for this run **under this model version**, riskiest first.

        Empty when the promoted model has changed since the run was last reported — which
        is the point: the caller then re-scores against what is promoted *now*.
        """
        stmt = (
            self._sa.select(self._units)
            .where(
                self._sa.and_(
                    self._units.c.run_id == run_id,
                    self._units.c.model_version == str(model_version),
                )
            )
            .order_by(self._units.c.risk.desc())
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [
            generate.UnitRisk(
                unit_id=r["unit_id"],
                n_rows=int(r["n_rows"]),
                risk=float(r["risk"]),
                peak=float(r["peak"]),
                high_risk_share=float(r["high_risk_share"]),
                flagged=bool(r["flagged"]),
            )
            for r in rows
        ]

    # --- retention ------------------------------------------------------------

    def total_readings(self) -> int:
        with self._engine.connect() as conn:
            return int(
                conn.execute(
                    self._sa.select(self._sa.func.count()).select_from(self._readings)
                ).scalar_one()
            )

    def prune(
        self,
        max_total_rows: int = generate.MAX_TOTAL_STORED_ROWS,
        *,
        keep: str | None = None,
    ) -> int:
        """Evict the oldest **finished** runs until the store is under the row budget.

        Returns the number of runs evicted. Two things are never evicted: an **in-flight**
        run (a user polling it must not have it deleted underneath them) and the run named
        by ``keep`` — the worker passes the run it has just finished, so the retention pass
        can never delete the very fleet it is about to hand back.
        """
        total = self.total_readings()
        if total <= max_total_rows:
            return 0

        stmt = (
            self._sa.select(self._runs.c.run_id, self._runs.c.n_rows)
            .where(self._runs.c.status.in_(TERMINAL_STATUSES))
            .order_by(self._runs.c.created_at.asc())
        )
        with self._engine.connect() as conn:
            candidates = conn.execute(stmt).all()

        evicted = 0
        for run_id, n_rows in candidates:
            if total <= max_total_rows:
                break
            if run_id == keep:
                continue
            self.delete_run(run_id)
            total -= int(n_rows or 0)
            evicted += 1
        return evicted

    def delete_run(self, run_id: str) -> None:
        with self._engine.begin() as conn:
            for table in (self._readings, self._units, self._runs):
                conn.execute(self._sa.delete(table).where(table.c.run_id == run_id))

    def dispose(self) -> None:
        self._engine.dispose()


def open_store(url: str | None = None) -> GenerationStore | None:
    """Open the generation store, or ``None`` when no database is configured.

    ``None`` is the honest "this feature is off" signal, not a broken state: the API then
    answers a 503 that *says* generation needs a database, and every other endpoint carries
    on. That is what keeps a local ``pdm serve``, the HF Space and CI working unchanged.
    """
    import os

    resolved = url if url is not None else os.environ.get(DATABASE_URL_ENV)
    if not resolved:
        return None
    try:
        return GenerationStore(resolved)
    except Exception:  # noqa: BLE001 — an unreachable DB must not break app startup
        return None


def _as_float_or_none(value: Any) -> float | None:
    """Era-NULL (and NaN) survive as JSON ``null`` — a real input, not a missing one."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


def _as_dict(value: Any) -> dict[str, float | None]:
    if isinstance(value, str):
        return json.loads(value)
    return dict(value) if value is not None else {}


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
