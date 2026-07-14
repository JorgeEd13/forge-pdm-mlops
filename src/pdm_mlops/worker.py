"""The generation worker — the second deployable unit (F14a, ADR-026 / decision S2).

This is what the Cloud Run Job runs, and it is the *only* place the forge is executed. It
takes a run that the API already wrote as ``queued``, generates the fleet, stores it, and
moves the run to a terminal state. The API polls that state; the two processes never talk
directly.

Everything here is written to be **crash-honest**. A worker that dies silently leaves a run
stuck in ``running`` forever and a user staring at a spinner, so every failure path — an
out-of-bounds spec, a missing run row, a generator error, a store error — ends with the run
marked ``failed`` and the reason attached to it.
"""

from __future__ import annotations

import os

from . import generate, store_gen

#: Env vars the Cloud Run Job execution is overridden with (see :mod:`jobs`).
RUN_ID_ENV = "RUN_ID"
UNITS_ENV = "GENERATION_UNITS"
DAYS_ENV = "GENERATION_DAYS"
SEED_ENV = "GENERATION_SEED"


def execute_run(
    run_id: str,
    spec: generate.GenerationSpec,
    store: store_gen.GenerationStore,
) -> int:
    """Generate, store, and close out one run. Returns the number of rows stored.

    The spec is **re-validated here**, not merely trusted from the API: the worker is
    separately invokable (that is the whole point of S2), so its own inputs are its own
    responsibility. The caps are a property of the system, not of one entry point.
    """
    run = store.get_run(run_id)
    if run is None:
        raise LookupError(f"no such generation run: {run_id!r}")

    store.mark_running(run_id)
    try:
        spec.validate()
        readings = generate.run_generation(spec)
        n_rows = store.append_readings(run_id, readings.loc[:, generate.stored_columns()])
        store.mark_succeeded(run_id, n_rows=n_rows)
    except Exception as exc:  # noqa: BLE001 — every failure must land on the run row
        store.mark_failed(run_id, error=f"{type(exc).__name__}: {exc}")
        raise

    # Retention runs *after* a successful run, so the free tier is defended at the moment
    # it grows — and never evicts the run we just handed the user (see store_gen.prune).
    store.prune(keep=run_id)
    return n_rows


def main(
    run_id: str | None = None,
    spec: generate.GenerationSpec | None = None,
    *,
    database_url: str | None = None,
) -> int:
    """CLI/job entry point: resolve the run from args or the job's env overrides.

    Exit code is what a Cloud Run Job execution reports, so a failed generation is visible
    in the cloud console as a failed execution — not only in the database.
    """
    run_id = run_id or os.environ.get(RUN_ID_ENV)
    if not run_id:
        print(f"error: no run id (pass --run-id or set {RUN_ID_ENV}).")
        return 2

    if spec is None:
        spec = generate.GenerationSpec(
            n_units=int(os.environ.get(UNITS_ENV, generate.DEFAULT_UNITS)),
            days=int(os.environ.get(DAYS_ENV, generate.DEFAULT_DAYS)),
            seed=int(os.environ.get(SEED_ENV, 42)),
        )

    store = store_gen.open_store(database_url)
    if store is None:
        # No database means no way to report the outcome to anyone — fail loudly here
        # rather than generating a fleet into the void.
        print(
            "error: the generation worker needs a database "
            f"({store_gen.DATABASE_URL_ENV} is unset or unreachable)."
        )
        return 2

    try:
        n_rows = execute_run(run_id, spec, store)
    except Exception as exc:  # noqa: BLE001 — already recorded on the run; report and exit
        print(f"generation run {run_id} FAILED: {type(exc).__name__}: {exc}")
        return 1
    finally:
        store.dispose()

    print(
        f"generation run {run_id} succeeded: {spec.n_units} vehicles × {spec.days} days "
        f"→ {n_rows:,} readings stored."
    )
    return 0
