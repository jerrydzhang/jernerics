"""JSON envelope shapes written to the local JSONL buffer and POSTed to /ingest.

One payload key (metric | param | result | artifact | sweep_meta | trial_end)
is present per envelope. These TypedDicts mirror the original tracking.proto
schema and are for documentation/type-checking; the runtime data is plain dict.
"""

from typing import TypedDict


class ParamValue(TypedDict, total=False):
    float_val: float
    int_val: int
    string_val: str
    bool_val: bool


class ParamEvent(TypedDict):
    key: str
    value: ParamValue


class MetricEvent(TypedDict, total=False):
    key: str
    value: float
    step: int


class ResultEvent(TypedDict):
    key: str
    value: str


class ArtifactEvent(TypedDict):
    key: str
    filename: str


class SweepMetaEvent(TypedDict, total=False):
    git_hash: str
    config: str


class TrialEndEvent(TypedDict):
    pass


class Envelope(TypedDict, total=False):
    project: str
    study_name: str
    trial_id: int
    timestamp_ns: int
    seq: int
    metric: MetricEvent
    param: ParamEvent
    result: ResultEvent
    artifact: ArtifactEvent
    sweep_meta: SweepMetaEvent
    trial_end: TrialEndEvent
