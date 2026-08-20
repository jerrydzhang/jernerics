"""Project-scoped selection, page, and query models."""

from pydantic import BaseModel, ConfigDict, Field

from .ids import ExecutionId, SweepId, TrialId


class Selection(BaseModel):
    """Which objects a query applies to: one project plus optional id filters."""

    model_config = ConfigDict(frozen=True)

    project: str
    sweeps: tuple[SweepId, ...] | None = None
    trials: tuple[TrialId, ...] | None = None
    retry_roots: tuple[TrialId, ...] | None = None
    executions: tuple[ExecutionId, ...] | None = None


class Page(BaseModel):
    """Offset paging for query responses."""

    model_config = ConfigDict(frozen=True)

    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class Query(BaseModel):
    """A domain query: a selection plus paging."""

    model_config = ConfigDict(frozen=True)

    selection: Selection
    page: Page = Field(default_factory=Page)
