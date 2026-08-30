"""Contract tests for the domain read wire request models."""

import uuid
from typing import Any

import pytest
from jernerics_schema import (
    ArtifactsQuery,
    ExecutionsQuery,
    JobResourcesQuery,
    LineageQuery,
    ProjectsQuery,
    ProvenanceQuery,
    Selection,
    SweepsQuery,
    TrialParamsQuery,
    TrialsQuery,
    ValueCatalogQuery,
    ValuesQuery,
)
from pydantic import ValidationError


def _selection() -> Selection:
    return Selection(project="proj")


def test_wire_queries_default_shape_and_roundtrip() -> None:
    queries = [
        ProjectsQuery(),
        SweepsQuery(selection=_selection()),
        TrialsQuery(selection=_selection()),
        TrialParamsQuery(selection=_selection()),
        LineageQuery(selection=_selection()),
        ExecutionsQuery(selection=_selection()),
        ValueCatalogQuery(selection=_selection()),
        ValuesQuery(selection=_selection()),
        ArtifactsQuery(selection=_selection()),
        ProvenanceQuery(selection=_selection()),
        JobResourcesQuery(selection=_selection()),
    ]
    for query in queries:
        assert type(query).model_validate_json(query.model_dump_json()) == query
    assert not TrialsQuery(selection=_selection()).retry_roots_only
    assert ExecutionsQuery(selection=_selection()).derive
    assert ValuesQuery(selection=_selection()).json_only is False


def test_wire_queries_are_frozen() -> None:
    query = TrialsQuery(selection=_selection())
    with pytest.raises(ValidationError):
        query.retry_roots_only = True


def test_values_query_filters() -> None:
    query = ValuesQuery(
        selection=_selection(),
        keys=("loss",),
        steps=(0, 1),
        since_ns=123,
        json_only=True,
        key="loss",
    )
    assert query.steps == (0, 1)
    with pytest.raises(ValidationError):
        ValuesQuery(selection=_selection(), steps=(-1,))
    with pytest.raises(ValidationError):
        ValuesQuery(selection=_selection(), since_ns=-1)


def test_executions_query_validates_states_and_threshold() -> None:
    selection = _selection()
    assert ExecutionsQuery(selection=selection, states=("running",)).states == (
        "running",
    )
    bad_states: Any = ("zombie",)
    with pytest.raises(ValidationError):
        ExecutionsQuery(selection=selection, states=bad_states)
    with pytest.raises(ValidationError):
        ExecutionsQuery(selection=selection, heartbeat_stale_s=0)


def test_trial_params_query_validates_kinds() -> None:
    bad_kinds: Any = ("derived",)
    with pytest.raises(ValidationError):
        TrialParamsQuery(selection=_selection(), kinds=bad_kinds)


def test_selection_serializes_uuids_in_queries() -> None:
    sweep_id = uuid.uuid4()
    query = SweepsQuery(selection=Selection(project="p", sweeps=(sweep_id,)))
    assert query.selection.sweeps == (sweep_id,)


def test_job_resources_query_defaults_and_job_ids() -> None:
    query = JobResourcesQuery(selection=_selection())

    assert query.job_ids is None
    assert query.page.limit == 100
    assert query.page_token is None
    named = JobResourcesQuery(selection=_selection(), job_ids=("123456",))
    assert named.job_ids == ("123456",)
