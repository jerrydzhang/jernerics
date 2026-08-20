"""Contract tests for the optimizer-neutral domain records."""

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from jernerics_schema import (
    ArtifactRecord,
    ExecutionOutcome,
    ExecutionRecord,
    JobRecord,
    ProvenanceRecord,
    SubmissionRecord,
    SubmissionState,
    SweepRecord,
    TrialLineageRecord,
    TrialParamRecord,
    TrialRecord,
    TrialState,
    ValueCatalogRecord,
    ValueRecord,
)
from pydantic import BaseModel, ValidationError

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def _records() -> list[BaseModel]:
    sweep_id = uuid.uuid4()
    submission_id = uuid.uuid4()
    trial_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    return [
        SweepRecord(sweep_id=sweep_id, project="proj", name="lr-sweep", state="active"),
        SubmissionRecord(
            submission_id=submission_id,
            sweep_id=sweep_id,
            backend="slurm",
            state=SubmissionState.SUBMITTED,
        ),
        JobRecord(
            job_id=uuid.uuid4(),
            submission_id=submission_id,
            scheduler_job_id="123456",
            state=SubmissionState.RUNNING,
        ),
        TrialRecord(
            trial_id=trial_id,
            sweep_id=sweep_id,
            number=0,
            state=TrialState.COMPLETED,
            retry_root_trial_id=trial_id,
        ),
        ExecutionRecord(
            execution_id=execution_id,
            trial_id=trial_id,
            hostname="node01",
            started_at=NOW,
            ended_at=NOW,
            outcome=ExecutionOutcome.SUCCESS,
            exit_code=0,
            last_heartbeat_ns=1_772_000_000_000_000_000,
            last_observation_ns=1_772_000_000_100_000_000,
            monitoring="ended",
        ),
        ValueRecord(trial_id=trial_id, key="loss", step=2, value=0.5),
        ValueRecord(
            execution_id=execution_id,
            trial_id=trial_id,
            key="pred",
            step=3,
            observation={"expr": "x ** 2"},
        ),
        ArtifactRecord(
            artifact_id=uuid.uuid4(),
            trial_id=trial_id,
            execution_id=execution_id,
            key="stdout",
            filename="trial-0.stdout",
            content_type="text/plain",
            size_bytes=16,
            source="system",
            received_ns=1_772_000_000_000_000_000,
        ),
        TrialParamRecord(
            trial_id=trial_id,
            kind="sampled",
            key="lr",
            value=0.1,
        ),
        TrialLineageRecord(
            trial_id=trial_id,
            retry_root_trial_id=trial_id,
            number=0,
            sweep_id=sweep_id,
        ),
        ValueCatalogRecord(
            key="loss",
            kind="scalar",
            n_points=4,
            latest_step=3,
            n_trials=2,
        ),
        ProvenanceRecord(
            submission_id=submission_id,
            sweep_id=sweep_id,
            backend="slurm",
            submitted_at_ns=1_772_000_000_000_000_000,
            expected_trials=12,
            git_hash="0" * 40,
            config_source="config.py",
        ),
    ]


@pytest.mark.parametrize("record", _records(), ids=lambda r: type(r).__name__)
def test_record_roundtrip(record: BaseModel) -> None:
    cls = type(record)
    assert cls.model_validate_json(record.model_dump_json()) == record


def test_records_are_frozen() -> None:
    record = SweepRecord(
        sweep_id=uuid.uuid4(), project="proj", name="lr-sweep", state="active"
    )
    with pytest.raises(ValidationError):
        record.name = "renamed"


def test_trial_record_lineage_defaults() -> None:
    trial_id = uuid.uuid4()
    record = TrialRecord(
        trial_id=trial_id,
        sweep_id=uuid.uuid4(),
        number=0,
        state=TrialState.WAITING,
        retry_root_trial_id=trial_id,
    )
    assert record.params.root == {}
    assert record.retry_of_trial_id is None
    assert record.retry_index == 0


def test_monitoring_rejects_unknown_label() -> None:
    bad_label: Any = "fresh"
    with pytest.raises(ValidationError):
        ExecutionRecord(
            execution_id=uuid.uuid4(),
            trial_id=uuid.uuid4(),
            hostname="node01",
            started_at=NOW,
            monitoring=bad_label,
        )
