"""Lifecycle and outcome enumerations for trials, submissions, and executions."""

from enum import Enum


class TrialState(str, Enum):
    WAITING = "waiting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PRUNED = "pruned"


class SubmissionState(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionOutcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    LOST = "lost"


class FailureKind(str, Enum):
    EXCEPTION = "exception"
    TIMEOUT = "timeout"
    STALE_HEARTBEAT = "stale_heartbeat"
    NODE_FAILURE = "node_failure"
    OOM = "oom"
    UNKNOWN = "unknown"
