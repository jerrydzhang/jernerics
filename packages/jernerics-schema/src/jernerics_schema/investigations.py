"""Investigation records: named sweep groups for factor/outcome questions."""

from pydantic import BaseModel, ConfigDict

from .ids import InvestigationId, SweepId
from .selection import Selection


class InvestigationRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: InvestigationId
    project: str
    name: str
    factor: str
    outcome: str
    replicate_factor: str | None = None
    archived_ns: int | None = None
    created_ns: int
    updated_ns: int
    members: tuple[SweepId, ...] = ()


def materialize_selection(inv: InvestigationRecord) -> Selection:
    """Selection scoping a query to the sweeps of an investigation."""
    return Selection(project=inv.project, sweeps=inv.members)
