"""Mirror optuna trial state into optimizer-neutral tracking snapshots.

The optuna journal stays authoritative for trial state and candidate
generation; these helpers translate a ``FrozenTrial`` (or the live trial
object, which exposes the same ``params``/``distributions``/``user_attrs``
surface) into generic ``TrialSnapshotEvent`` payloads without leaking
optuna types into the tracking contracts.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jernerics_schema import (
    JERNERICS_NAMESPACE,
    FlatContext,
    ScalarValue,
    SweepId,
    TrialId,
    TrialSnapshotEvent,
    TrialState,
    sweep_id_for,
)
from optuna.distributions import distribution_to_json
from optuna.trial import FrozenTrial
from optuna.trial import TrialState as OptunaState

TRIAL_ID_ATTR = "jernerics_trial_id"
"""User attr linking an optimizer trial to its tracking identity."""

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_STATE_MAP = {
    OptunaState.COMPLETE: TrialState.COMPLETED,
    OptunaState.FAIL: TrialState.FAILED,
    OptunaState.PRUNED: TrialState.PRUNED,
    OptunaState.RUNNING: TrialState.RUNNING,
    OptunaState.WAITING: TrialState.WAITING,
}


def flat_params(params: dict[str, Any]) -> dict[str, ScalarValue]:
    """Only flat scalars fit TrialSnapshotEvent.params; richer values drop."""
    return {
        key: value
        for key, value in params.items()
        if isinstance(value, str | int | float | bool) or value is None
    }


def flat_distributions(distributions: dict[str, Any]) -> FlatContext:
    """Each distribution becomes its canonical optuna JSON string."""
    return FlatContext(
        {name: distribution_to_json(dist) for name, dist in distributions.items()}
    )


def flat_attrs(attrs: dict[str, Any]) -> FlatContext:
    """Merged user attrs; non-scalar values are JSON-stringified."""
    return FlatContext(
        {
            key: (
                value
                if isinstance(value, str | int | float | bool) or value is None
                else json.dumps(value, sort_keys=True)
            )
            for key, value in attrs.items()
        }
    )


def _trial_id_from(value: Any) -> TrialId | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _int_from(value: Any, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


@dataclass(frozen=True)
class TrialLineage:
    retry_of_trial_id: TrialId | None
    retry_root_trial_id: TrialId
    retry_index: int


def lineage_from_attrs(attrs: dict[str, Any], own_trial_id: TrialId) -> TrialLineage:
    """Lineage carried in enqueue-time user attrs; absent ids are never invented."""
    retry_index = max(0, _int_from(attrs.get("retry_index"), 0))
    retry_of = _trial_id_from(attrs.get("retry_of_trial_id"))
    root = _trial_id_from(attrs.get("retry_root_trial_id"))
    if (
        root is None
        and retry_of is not None
        and attrs.get("retry_root") == attrs.get("retry_of")
    ):
        # Only trust the parent id as the root when the attrs say root == parent.
        root = retry_of
    return TrialLineage(
        retry_of_trial_id=retry_of,
        retry_root_trial_id=root if root is not None else own_trial_id,
        retry_index=retry_index,
    )


def snapshot_kwargs(trial: Any, *, trial_id: TrialId) -> dict[str, Any]:
    """Keyword arguments for ``Tracker.emit_trial_snapshot`` mirroring a trial."""
    attrs = dict(trial.user_attrs)
    lineage = lineage_from_attrs(attrs, trial_id)
    return {
        "params": flat_params(trial.params),
        "distributions": flat_distributions(trial.distributions),
        "attrs": flat_attrs(attrs),
        "retry_of_trial_id": lineage.retry_of_trial_id,
        "retry_root_trial_id": lineage.retry_root_trial_id,
        "retry_index": lineage.retry_index,
    }


def fallback_trial_id(sweep_id: SweepId, number: int) -> TrialId:
    """Deterministic identity for a trial that never recorded a live one.

    Derived from the schema namespace so repeated post-hook reconciliations
    agree on the id and re-reconciles are duplicates, not new trials.
    """
    return uuid.uuid5(JERNERICS_NAMESPACE, f"trial:{sweep_id}:{number}")


def tracked_trial_id(attrs: dict[str, Any], sweep_id: SweepId, number: int) -> TrialId:
    live = _trial_id_from(attrs.get(TRIAL_ID_ATTR))
    return live if live is not None else fallback_trial_id(sweep_id, number)


def _journal_timestamp(trial: FrozenTrial) -> datetime:
    # Optuna stores naive local datetimes; WAITING trials carry none at all,
    # so the epoch keeps repeated reconciles byte-identical.
    stamp = trial.datetime_complete or trial.datetime_start
    if stamp is None:
        return _EPOCH
    return stamp if stamp.tzinfo is not None else stamp.astimezone()


def frozen_trial_snapshot(
    trial: FrozenTrial,
    *,
    sweep_id: SweepId,
    recorded_at: datetime | None = None,
    event_id: uuid.UUID | None = None,
) -> TrialSnapshotEvent:
    """Snapshot event mirroring a FrozenTrial.

    Default arguments derive both timestamps (from the journal) and the event
    id deterministically, so reconciliation replays are duplicates. Callers
    emitting a live observation (the retry checker) pass ``recorded_at`` and
    ``event_id`` explicitly.
    """
    attrs = dict(trial.user_attrs)
    trial_id = tracked_trial_id(attrs, sweep_id, trial.number)
    lineage = lineage_from_attrs(attrs, trial_id)
    return TrialSnapshotEvent(
        event_id=(
            event_id
            if event_id is not None
            else uuid.uuid5(JERNERICS_NAMESPACE, f"reconcile:{sweep_id}:{trial.number}")
        ),
        recorded_at=(
            recorded_at if recorded_at is not None else _journal_timestamp(trial)
        ),
        trial_id=trial_id,
        sweep_id=sweep_id,
        number=trial.number,
        state=_STATE_MAP[trial.state],
        params=FlatContext(flat_params(trial.params)),
        objective=(trial.value if isinstance(trial.value, int | float) else None),
        distributions=flat_distributions(trial.distributions),
        attrs=flat_attrs(attrs),
        retry_of_trial_id=lineage.retry_of_trial_id,
        retry_root_trial_id=lineage.retry_root_trial_id,
        retry_index=lineage.retry_index,
    )


def retry_lineage_attrs(original: FrozenTrial, root: FrozenTrial) -> dict[str, Any]:
    """User attrs enqueued on a retry so the runner can mirror its lineage."""
    attrs = dict(original.user_attrs)
    root_attrs = dict(root.user_attrs)
    lineage: dict[str, Any] = {
        "retry_of": original.number,
        "retry_root": root.number,
        "retry_index": _int_from(attrs.get("retry_index"), 0) + 1,
    }
    retry_of_trial_id = _trial_id_from(attrs.get(TRIAL_ID_ATTR))
    if retry_of_trial_id is not None:
        lineage["retry_of_trial_id"] = str(retry_of_trial_id)
    root_trial_id = _trial_id_from(root_attrs.get(TRIAL_ID_ATTR))
    if root_trial_id is not None:
        lineage["retry_root_trial_id"] = str(root_trial_id)
    return lineage


def sweep_identity(project_name: str | None, study_name: str) -> SweepId:
    return sweep_id_for(project_name or "", study_name)
