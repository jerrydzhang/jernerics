from typing import TypedDict


class ParamValue(TypedDict, total=False):
    float_val: float
    int_val: int
    string_val: str
    bool_val: bool


class ParamEvent(TypedDict):
    key: str
    value: ParamValue


class ValueEvent(TypedDict, total=False):
    key: str
    value: float
    value_json: str
    step: int
    context: dict


class ArtifactEvent(TypedDict, total=False):
    key: str
    filename: str
    context: dict


class SweepMetaEvent(TypedDict, total=False):
    git_hash: str
    config: str


class TrialEndEvent(TypedDict):
    pass


class Envelope(TypedDict, total=False):
    project: str
    study_name: str
    trial_id: int
    run_id: int
    timestamp_ns: int
    seq: int
    value: ValueEvent
    param: ParamEvent
    artifact: ArtifactEvent
    sweep_meta: SweepMetaEvent
    trial_end: TrialEndEvent
