"""Wire request models for the typed domain read API."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .events import ArtifactSource
from .selection import Page, Selection

Step = Annotated[int, Field(ge=0)]


class ProjectsQuery(BaseModel):
    """Project catalog request; carries no filters today."""

    model_config = ConfigDict(frozen=True)


class SweepsQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    selection: Selection
    states: tuple[str, ...] | None = None
    page: Page = Field(default_factory=Page)
    page_token: str | None = None


class TrialsQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    selection: Selection
    states: tuple[str, ...] | None = None
    retry_roots_only: bool = False
    page: Page = Field(default_factory=Page)
    page_token: str | None = None


class TrialParamsQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    selection: Selection
    kinds: tuple[Literal["sampled", "manual"], ...] | None = None
    page: Page = Field(default_factory=Page)
    page_token: str | None = None


class LineageQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    selection: Selection


class ExecutionsQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    selection: Selection
    states: tuple[Literal["running", "ended"], ...] | None = None
    derive: bool = True
    heartbeat_stale_s: float | None = Field(default=None, gt=0)


class ValueCatalogQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    selection: Selection


class ValuesQuery(BaseModel):
    """Value scan; ``key`` narrows to a single-key series."""

    model_config = ConfigDict(frozen=True)

    selection: Selection
    keys: tuple[str, ...] | None = None
    steps: tuple[Step, ...] | None = None
    since_ns: int | None = Field(default=None, ge=0)
    json_only: bool = False
    key: str | None = None
    page: Page = Field(default_factory=Page)
    page_token: str | None = None


class ArtifactsQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    selection: Selection
    keys: tuple[str, ...] | None = None
    received: bool | None = None
    source: ArtifactSource | None = None
    page: Page = Field(default_factory=Page)
    page_token: str | None = None


class ProvenanceQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    selection: Selection


class QueryErrorBody(BaseModel):
    """Structured error body shared by every domain read endpoint."""

    model_config = ConfigDict(frozen=True)

    code: str
    detail: str = ""


class QueryErrorResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    error: QueryErrorBody
