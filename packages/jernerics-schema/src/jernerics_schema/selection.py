"""Project-scoped selection, page, and query models."""

import base64
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .ids import ExecutionId, SweepId, TrialId


class Selection(BaseModel):
    """Which objects a query applies to: one project plus optional id filters.

    Filters compose conjunctively across dimensions (project, sweeps,
    trials/retry-roots/executions) and additively within the id dimension:
    a trial is selected when it is named by ``trials``, belongs to a retry
    family named by ``retry_roots`` (family expansion), or holds an
    execution named by ``executions``.
    """

    model_config = ConfigDict(frozen=True)

    project: str
    sweeps: tuple[SweepId, ...] | None = None
    trials: tuple[TrialId, ...] | None = None
    retry_roots: tuple[TrialId, ...] | None = None
    executions: tuple[ExecutionId, ...] | None = None


class Page(BaseModel):
    """Paging request: limit plus either a keyset token or offset 0."""

    model_config = ConfigDict(frozen=True)

    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class PageToken(BaseModel):
    """Opaque keyset cursor payload carried inside a page token."""

    model_config = ConfigDict(frozen=True)

    cursor: tuple[int | str, ...] = ()
    limit: int = Field(ge=1, le=1000)
    filters: dict[str, Any] = Field(default_factory=dict)


def encode_page_token(token: PageToken) -> str:
    """Encode a token as opaque base64url JSON (no crypto: read paging only)."""
    payload = token.model_dump_json().encode("ascii")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_page_token(value: str) -> PageToken:
    """Decode a page token; raises ValueError when malformed."""
    try:
        padded = value + "=" * (-len(value) % 4)
        return PageToken.model_validate_json(base64.urlsafe_b64decode(padded))
    except ValueError as e:
        raise ValueError(f"malformed page token: {e}") from e


class Query(BaseModel):
    """A domain query: a selection plus paging."""

    model_config = ConfigDict(frozen=True)

    selection: Selection
    page: Page = Field(default_factory=Page)
