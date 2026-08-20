"""Retry lineage shared by trial snapshots and trial records."""

from pydantic import BaseModel, ConfigDict, Field

from .ids import TrialId


class RetryLineage(BaseModel):
    """Where a trial sits in its retry chain.

    A first-attempt trial is its own root with ``retry_index == 0`` and no
    ``retry_of_trial_id``.
    """

    model_config = ConfigDict(frozen=True)

    retry_of_trial_id: TrialId | None = None
    retry_root_trial_id: TrialId
    retry_index: int = Field(default=0, ge=0)
