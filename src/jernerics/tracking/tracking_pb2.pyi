# type: ignore
# ruff: noqa
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar

from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message

DESCRIPTOR: _descriptor.FileDescriptor

class Envelope(_message.Message):
    __slots__ = (
        "artifact",
        "metric",
        "param",
        "result",
        "study_name",
        "sweep_meta",
        "timestamp_ns",
        "trial_id",
    )
    STUDY_NAME_FIELD_NUMBER: _ClassVar[int]
    TRIAL_ID_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_NS_FIELD_NUMBER: _ClassVar[int]
    SWEEP_META_FIELD_NUMBER: _ClassVar[int]
    PARAM_FIELD_NUMBER: _ClassVar[int]
    METRIC_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    ARTIFACT_FIELD_NUMBER: _ClassVar[int]
    study_name: str
    trial_id: int
    timestamp_ns: int
    sweep_meta: SweepMetaEvent
    param: ParamEvent
    metric: MetricEvent
    result: ResultEvent
    artifact: ArtifactEvent
    def __init__(
        self,
        study_name: str | None = ...,
        trial_id: int | None = ...,
        timestamp_ns: int | None = ...,
        sweep_meta: SweepMetaEvent | _Mapping | None = ...,
        param: ParamEvent | _Mapping | None = ...,
        metric: MetricEvent | _Mapping | None = ...,
        result: ResultEvent | _Mapping | None = ...,
        artifact: ArtifactEvent | _Mapping | None = ...,
    ) -> None: ...

class SweepMetaEvent(_message.Message):
    __slots__ = ("config", "git_hash")
    GIT_HASH_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    git_hash: str
    config: str
    def __init__(
        self, git_hash: str | None = ..., config: str | None = ...
    ) -> None: ...

class Value(_message.Message):
    __slots__ = ("bool_val", "float_val", "int_val", "string_val")
    FLOAT_VAL_FIELD_NUMBER: _ClassVar[int]
    INT_VAL_FIELD_NUMBER: _ClassVar[int]
    STRING_VAL_FIELD_NUMBER: _ClassVar[int]
    BOOL_VAL_FIELD_NUMBER: _ClassVar[int]
    float_val: float
    int_val: int
    string_val: str
    bool_val: bool
    def __init__(
        self,
        float_val: float | None = ...,
        int_val: int | None = ...,
        string_val: str | None = ...,
        bool_val: bool | None = ...,
    ) -> None: ...

class ParamEvent(_message.Message):
    __slots__ = ("key", "value")
    KEY_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    key: str
    value: Value
    def __init__(
        self, key: str | None = ..., value: Value | _Mapping | None = ...
    ) -> None: ...

class MetricEvent(_message.Message):
    __slots__ = ("key", "step", "value")
    KEY_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    STEP_FIELD_NUMBER: _ClassVar[int]
    key: str
    value: float
    step: int
    def __init__(
        self, key: str | None = ..., value: float | None = ..., step: int | None = ...
    ) -> None: ...

class ResultEvent(_message.Message):
    __slots__ = ("key", "value")
    KEY_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    key: str
    value: str
    def __init__(self, key: str | None = ..., value: str | None = ...) -> None: ...

class ArtifactEvent(_message.Message):
    __slots__ = ("key", "local_path")
    KEY_FIELD_NUMBER: _ClassVar[int]
    LOCAL_PATH_FIELD_NUMBER: _ClassVar[int]
    key: str
    local_path: str
    def __init__(self, key: str | None = ..., local_path: str | None = ...) -> None: ...
