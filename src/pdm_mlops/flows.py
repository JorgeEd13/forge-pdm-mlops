"""The drift → auto-retrain loop — the marquee (F5).

This is the capability the repo exists to demonstrate: a **closed loop** where a
distribution shift is detected, a fresh model is trained on the shifted data, and — only
if it clears the same governance gate that guards every promotion — it is promoted to
production, with no human in the path. Nothing here is new *mechanism*; it is the F5
*composition* of the spine already built:

    detect_drift (F5 monitor) → [if drift] → retrain (F2 train) →
        evaluate + promote-or-hold (F3 registry, the SAME gate)

The gate is the load-bearing honesty: the loop cannot silently ship a worse model,
because the promotion step is F3's :func:`registry.promote` unchanged — a candidate that
does not beat the incumbent is *held*, and that is a normal, reported outcome, not a
failure. So "auto-retrain" never means "auto-degrade".

**Prefect is the flow author/executor; GitHub Actions is only the scheduler**
(``.github/workflows/retrain.yml``) — they layer, they do not duplicate (ADR-002). The
flow runs **in-process** (Prefect's default local runner) so the tests exercise the real
task graph on the fixture with no server. Prefect lives in the optional ``[ops]`` extra
and is imported lazily, so importing the package and running core CI stays light.

Everything is injectable (the readings frames, the tracking URI) so the whole loop runs
offline on the committed fixture against a throwaway SQLite registry.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from . import config, monitor, registry, train


@dataclass(frozen=True)
class FlowResult:
    """The outcome of one drift → retrain cycle.

    ``drift`` is the monitor's decision (with its evidence). ``retrained`` says whether
    the retrain branch fired (it only does when drift was detected). ``promotion`` is the
    F3 gate's structured decision when a retrain happened (``None`` when no drift, so no
    candidate was produced). ``promoted`` is the convenience flag: a model actually
    reached production this cycle.
    """

    drift: monitor.DriftReport
    retrained: bool
    promotion: registry.PromotionResult | None
    promoted: bool

    def summary(self) -> str:
        lines = [self.drift.summary()]
        if not self.retrained:
            lines.append("No retrain: data is stable, production model kept.")
        else:
            assert self.promotion is not None
            lines.append(registry.format_promotion(self.promotion))
        return "\n".join(lines)


def _lazy_prefect():
    """Import Prefect's ``task``/``flow`` decorators (the ``[ops]`` extra), lazily.

    Kept behind a call so importing :mod:`flows` (and the CLI that wires it) does not
    require Prefect — only actually *running* the flow does. Raises a clear, actionable
    error if the extra is missing.
    """
    try:
        from prefect import flow, task
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via the CLI path
        raise ModuleNotFoundError(
            "the drift → retrain flow needs Prefect; install the '[ops]' extra "
            "(`pip install -e .[ops]`) — see docs/ROADMAP.md (F5)."
        ) from exc
    return flow, task


def run_drift_retrain(
    *,
    season: str | None = None,
    seed: int | None = None,
    tracking_uri: str | None = None,
    min_delta: float | None = None,
    reference: pd.DataFrame | None = None,
    current: pd.DataFrame | None = None,
) -> FlowResult:
    """Run one drift → retrain → gated-promote cycle as a Prefect flow, in-process.

    The steps are Prefect *tasks* (each retried once, so a transient MLflow/IO hiccup
    doesn't fail the cycle) composed by a Prefect *flow*; the flow object is built here so
    the decorators are only imported when the loop actually runs.

    Args:
        season: the generator season used as the drift stimulus (defaults to
            :data:`config.DRIFT_SEASON`); ignored when ``current`` is injected.
        seed: threads the retrain's data split → models (defaults to
            :data:`config.DEFAULT_SEED`).
        tracking_uri: MLflow tracking/registry URI; defaults to the local SQLite
            backend. Tests inject a tmp URI so the registry is throwaway.
        min_delta: the promotion gate tolerance passed straight to
            :func:`registry.promote` (defaults to :data:`registry.DEFAULT_MIN_DELTA`).
        reference / current: inject the baseline / shifted readings to run offline on
            the fixture (tests); the real path regenerates both from the generator.

    Returns:
        A :class:`FlowResult`: the drift decision, whether a retrain fired, and the F3
        promotion outcome (with ``promoted`` the convenience flag).
    """
    flow, task = _lazy_prefect()

    if seed is None:
        seed = config.DEFAULT_SEED
    if min_delta is None:
        min_delta = registry.DEFAULT_MIN_DELTA

    @task(retries=1)
    def detect_drift_task() -> monitor.DriftReport:
        return monitor.detect_drift(season=season, reference=reference, current=current)

    @task(retries=1)
    def retrain_task() -> str:
        """Retrain on the (shifted) data and register the winner → its version.

        The retrain trains on ``current`` when injected (the drifted window the tests
        supply), else the real full-regeneration path under the drift ``season`` — so
        the candidate is fit on the data that drifted, which is the whole point.
        """
        summary = train.train(
            seed=seed,
            tracking_uri=tracking_uri,
            register=True,
            readings=current,
        )
        return str(summary.registered_version)

    @task(retries=1)
    def promote_task(version: str) -> registry.PromotionResult:
        """The F3 gate, unchanged — a worse candidate is held, not shipped."""
        client = registry._client(tracking_uri)
        return registry.promote(
            client, config.REGISTERED_MODEL_NAME, version, min_delta=min_delta
        )

    @flow(name="drift-retrain")
    def _flow() -> FlowResult:
        drift = detect_drift_task()
        if not drift.drifted:
            return FlowResult(
                drift=drift, retrained=False, promotion=None, promoted=False
            )
        version = retrain_task()
        promotion = promote_task(version)
        return FlowResult(
            drift=drift,
            retrained=True,
            promotion=promotion,
            promoted=promotion.promoted,
        )

    return _flow()
