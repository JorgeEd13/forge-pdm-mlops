"""Registry layer — governed model lifecycle: gated promotion + rollback (F3).

F2 already *registers* the winning model version in the MLflow Model Registry. F3 is
the governance on top of that: which registered version is **in production**, moving
that pointer **only when a candidate clears an eval-metric gate**, and being able to
**roll back** to the version that was live before. This is the MLOps spine the repo
exists to close — "how does a new model actually reach production, and how do you undo
it" answered as tested, reproducible mechanism rather than a manual click.

**Aliases, not deprecated stages (ADR-008).** MLflow 3 deprecated the classic
`Staging`/`Production` *stage* transitions in favour of **model-version aliases**. So
"production" here is an **alias** (:data:`PRODUCTION_ALIAS`) that points at exactly one
version. Promotion re-points that alias; rollback re-points it back. The version it
supersedes is recorded as a tag (:data:`PREV_TAG`) on the newly-promoted version, so
rollback is deterministic and fully offline-testable — no run history scraping.

**The gate.** A candidate is compared against the incumbent production version on the
primary metric (:data:`config.PRIMARY_METRIC`, ROC-AUC), read from each version's source
run. It promotes only if ``candidate >= incumbent - min_delta`` — a strictly worse
candidate does **not** promote (the F3 DoD). A rejected promotion is a *normal governed
outcome*, not an error: it returns a structured :class:`PromotionResult` explaining why.
Genuinely broken inputs (a missing version, an unscored run) raise.

Everything runs against the same local **SQLite** MLflow backend as F2 (ADR-004); tests
point it at a tmp file. No new dependency.
"""

from __future__ import annotations

from dataclasses import dataclass

from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient

from . import config

#: The alias that marks the one version currently serving in production. Serving (F4)
#: loads ``models:/<name>@production``; promotion/rollback move this pointer.
PRODUCTION_ALIAS: str = "production"

#: Tag written on a freshly-promoted version recording the version it superseded, so
#: :func:`rollback` can restore the previous production version deterministically.
PREV_TAG: str = "superseded_production_version"

#: Default gate tolerance. A candidate must score within ``min_delta`` of the incumbent
#: to promote; the default ``0.0`` means "must be at least as good" (ties promote — the
#: newer version wins on equal evidence, a deliberate, documented choice).
DEFAULT_MIN_DELTA: float = 0.0


class PromotionError(RuntimeError):
    """A promotion could not even be *evaluated* (broken input, not a failed gate).

    Distinct from a gate *rejection*: a worse candidate not promoting is a normal,
    reported outcome (:class:`PromotionResult` with ``promoted=False``). This is raised
    only when the request itself is malformed — an unknown version, or a version whose
    source run never logged the primary metric so the gate has nothing to compare.
    """


@dataclass(frozen=True)
class PromotionResult:
    """The outcome of a gated promotion attempt.

    ``promoted`` says whether the production alias moved. ``candidate_metric`` /
    ``incumbent_metric`` are the compared ROC-AUCs (the incumbent is ``None`` on the
    first-ever promotion, when there is nothing to beat). ``reason`` is a short,
    human-readable explanation for the CLI and the audit trail.
    """

    name: str
    candidate_version: str
    candidate_metric: float
    incumbent_version: str | None
    incumbent_metric: float | None
    promoted: bool
    reason: str


def _get_version(client: MlflowClient, name: str, version: str):
    try:
        return client.get_model_version(name, version)
    except MlflowException as exc:  # unknown model or version
        raise PromotionError(
            f"model '{name}' version {version} not found in the registry"
        ) from exc


def production_version(client: MlflowClient, name: str) -> str | None:
    """The version the ``production`` alias points at, or ``None`` if unset.

    A missing alias (nothing promoted yet) is a normal state, not an error, so this
    swallows the MLflow "alias not found" and returns ``None``.
    """
    try:
        return str(client.get_model_version_by_alias(name, PRODUCTION_ALIAS).version)
    except MlflowException:
        return None


def latest_version(client: MlflowClient, name: str) -> str:
    """The highest-numbered registered version of ``name`` (the one just trained).

    ``get_latest_versions`` is stage-tied and deprecated in MLflow 3, so "latest" is
    resolved as the max integer version from a registry search. Raises if the model
    has no versions yet (nothing has been trained/registered).
    """
    versions = client.search_model_versions(f"name='{name}'")
    if not versions:
        raise PromotionError(
            f"registered model '{name}' has no versions; run `pdm train` first"
        )
    return str(max(versions, key=lambda mv: int(mv.version)).version)


def version_metric(client: MlflowClient, name: str, version: str) -> float:
    """The primary metric (ROC-AUC) a registered version was scored at.

    Read from the version's **source run** — the run ``train`` logged
    :data:`config.PRIMARY_METRIC` on before registering. Raises
    :class:`PromotionError` if the version has no run or that run never logged the
    metric, so the gate never silently compares against a missing number.
    """
    mv = _get_version(client, name, version)
    run_id = mv.run_id
    if not run_id:
        raise PromotionError(
            f"version {version} of '{name}' has no source run; cannot read its "
            f"{config.PRIMARY_METRIC} for the promotion gate"
        )
    run = client.get_run(run_id)
    metric = run.data.metrics.get(config.PRIMARY_METRIC)
    if metric is None:
        raise PromotionError(
            f"source run {run_id} of '{name}' v{version} did not log "
            f"'{config.PRIMARY_METRIC}'; the promotion gate has nothing to compare"
        )
    return float(metric)


def promote(
    client: MlflowClient,
    name: str,
    version: str,
    *,
    gate: bool = True,
    min_delta: float = DEFAULT_MIN_DELTA,
) -> PromotionResult:
    """Promote ``version`` to production, gated on the primary metric.

    Compares the candidate against the current production version (if any) on ROC-AUC.
    With ``gate=True`` the alias moves **only** if
    ``candidate >= incumbent - min_delta``; a strictly worse candidate is rejected and
    the alias is left untouched. The first-ever promotion (no incumbent) always passes
    the gate — there is nothing to protect yet. ``gate=False`` forces the move (the
    ``--force`` escape hatch) but still records the comparison.

    On a successful move, the superseded version is tagged on the new one
    (:data:`PREV_TAG`) so :func:`rollback` can restore it deterministically.

    Args:
        client: an ``MlflowClient`` bound to the registry backend.
        name: the registered model name (:data:`config.REGISTERED_MODEL_NAME`).
        version: the candidate version to consider for production.
        gate: enforce the metric gate (default). ``False`` promotes unconditionally.
        min_delta: gate tolerance — the candidate must be within this of the incumbent.

    Returns:
        A :class:`PromotionResult` describing the decision (``promoted`` True/False).

    Raises:
        PromotionError: the candidate version is unknown or has no logged metric.
    """
    # Normalise to str: MLflow hands versions back as int on some code paths and str on
    # others, so the registry surface speaks str throughout for stable comparisons.
    version = str(version)
    candidate_metric = version_metric(client, name, version)
    incumbent = production_version(client, name)

    incumbent_metric: float | None = None
    if incumbent is not None and incumbent != version:
        incumbent_metric = version_metric(client, name, incumbent)

    # Decide.
    if not gate:
        promoted, reason = True, "forced (gate bypassed)"
    elif incumbent is None:
        promoted, reason = True, "first production version (no incumbent to beat)"
    elif incumbent == version:
        promoted, reason = True, "already the production version"
    elif candidate_metric >= incumbent_metric - min_delta:
        promoted = True
        reason = (
            f"passed gate: {config.PRIMARY_METRIC} {candidate_metric:.4f} "
            f">= {incumbent_metric:.4f} - {min_delta:g}"
        )
    else:
        promoted = False
        reason = (
            f"rejected by gate: {config.PRIMARY_METRIC} {candidate_metric:.4f} "
            f"< incumbent {incumbent_metric:.4f} - {min_delta:g} "
            f"(v{incumbent} stays in production)"
        )

    if promoted:
        # Record what we're superseding *before* moving the alias, so rollback is
        # deterministic. Only tag when there is a distinct incumbent to return to.
        if incumbent is not None and incumbent != version:
            client.set_model_version_tag(name, version, PREV_TAG, incumbent)
        client.set_registered_model_alias(name, PRODUCTION_ALIAS, version)

    return PromotionResult(
        name=name,
        candidate_version=version,
        candidate_metric=candidate_metric,
        incumbent_version=incumbent,
        incumbent_metric=incumbent_metric,
        promoted=promoted,
        reason=reason,
    )


def rollback(client: MlflowClient, name: str) -> str:
    """Restore the version that was in production before the current one.

    Reads the :data:`PREV_TAG` tag on the current production version (written by
    :func:`promote`), re-points the ``production`` alias at it, and returns the
    restored version. Raises if nothing is promoted or the current version has no
    recorded predecessor (a first-ever promotion has nothing to roll back to).
    """
    current = production_version(client, name)
    if current is None:
        raise PromotionError(f"'{name}' has no production version to roll back from")

    mv = _get_version(client, name, current)
    prev = mv.tags.get(PREV_TAG)
    if not prev:
        raise PromotionError(
            f"'{name}' v{current} has no recorded predecessor ({PREV_TAG}); "
            "nothing to roll back to (it was the first production version)"
        )

    # Verify the predecessor still exists before pointing the alias at it.
    _get_version(client, name, prev)
    client.set_registered_model_alias(name, PRODUCTION_ALIAS, prev)
    return prev


def _client(tracking_uri: str | None = None) -> MlflowClient:
    """An ``MlflowClient`` on the F2 SQLite backend (or an injected tmp URI in tests).

    With no explicit URI it uses :func:`config.default_tracking_uri`, which honours the
    ``MLFLOW_TRACKING_URI`` env var (so the F4 serving container reads a mounted
    registry) and otherwise falls back to the local ``mlruns/`` SQLite backend.
    """
    if tracking_uri is None:
        tracking_uri = config.default_tracking_uri()
    return MlflowClient(tracking_uri=tracking_uri, registry_uri=tracking_uri)


def format_promotion(result: PromotionResult) -> str:
    """A compact, human-readable report of a promotion decision for the CLI."""
    head = "PROMOTED" if result.promoted else "REJECTED"
    inc = (
        f"v{result.incumbent_version} @ {result.incumbent_metric:.4f}"
        if result.incumbent_version is not None
        else "(none)"
    )
    return (
        f"[{head}] '{result.name}' candidate v{result.candidate_version} "
        f"@ {config.PRIMARY_METRIC} {result.candidate_metric:.4f}  "
        f"vs incumbent {inc}\n  {result.reason}"
    )
