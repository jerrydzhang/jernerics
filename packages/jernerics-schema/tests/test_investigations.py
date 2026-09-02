"""Contract tests for investigation records and selection materialization."""

import uuid

import pytest
from jernerics_schema import (
    InvestigationId,
    InvestigationRecord,
    Selection,
    materialize_selection,
)
from pydantic import ValidationError


def _record() -> InvestigationRecord:
    return InvestigationRecord(
        id=uuid.uuid4(),
        project="proj",
        name="lr-vs-batch",
        factor="learning_rate",
        outcome="val_loss",
        created_ns=1_000,
        updated_ns=2_000,
        members=(uuid.uuid4(), uuid.uuid4()),
    )


def test_record_json_roundtrip() -> None:
    record = _record()
    assert InvestigationRecord.model_validate_json(record.model_dump_json()) == record


def test_default_members_are_empty_tuple() -> None:
    record = InvestigationRecord(
        id=uuid.uuid4(),
        project="proj",
        name="bare",
        factor="quantization",
        outcome="perplexity",
        created_ns=0,
        updated_ns=0,
    )
    assert record.members == ()
    assert record.replicate_factor is None
    assert record.archived_ns is None


def test_members_are_immutable_tuple() -> None:
    record = _record()
    assert isinstance(record.members, tuple)
    with pytest.raises(ValidationError):
        record.members = ()


def test_investigation_id_accepts_uuid_strings() -> None:
    investigation_id: InvestigationId = uuid.uuid4()
    record = InvestigationRecord.model_validate(
        {
            "id": str(investigation_id),
            "project": "proj",
            "name": "n",
            "factor": "f",
            "outcome": "o",
            "created_ns": 1,
            "updated_ns": 2,
        }
    )
    assert record.id == investigation_id


def test_materialize_selection() -> None:
    record = _record()
    assert materialize_selection(record) == Selection(
        project=record.project, sweeps=record.members
    )
