from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Envelope(_message.Message):
    __slots__ = ("project", "study_name", "trial_id", "timestamp_ns", "seq", "sweep_meta", "param", "metric", "result", "artifact", "trial_end")
    PROJECT_FIELD_NUMBER: _ClassVar[int]
    STUDY_NAME_FIELD_NUMBER: _ClassVar[int]
    TRIAL_ID_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_NS_FIELD_NUMBER: _ClassVar[int]
    SEQ_FIELD_NUMBER: _ClassVar[int]
    SWEEP_META_FIELD_NUMBER: _ClassVar[int]
    PARAM_FIELD_NUMBER: _ClassVar[int]
    METRIC_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    ARTIFACT_FIELD_NUMBER: _ClassVar[int]
    TRIAL_END_FIELD_NUMBER: _ClassVar[int]
    project: str
    study_name: str
    trial_id: int
    timestamp_ns: int
    seq: int
    sweep_meta: SweepMetaEvent
    param: ParamEvent
    metric: MetricEvent
    result: ResultEvent
    artifact: ArtifactEvent
    trial_end: TrialEndEvent
    def __init__(self, project: _Optional[str] = ..., study_name: _Optional[str] = ..., trial_id: _Optional[int] = ..., timestamp_ns: _Optional[int] = ..., seq: _Optional[int] = ..., sweep_meta: _Optional[_Union[SweepMetaEvent, _Mapping]] = ..., param: _Optional[_Union[ParamEvent, _Mapping]] = ..., metric: _Optional[_Union[MetricEvent, _Mapping]] = ..., result: _Optional[_Union[ResultEvent, _Mapping]] = ..., artifact: _Optional[_Union[ArtifactEvent, _Mapping]] = ..., trial_end: _Optional[_Union[TrialEndEvent, _Mapping]] = ...) -> None: ...

class SweepMetaEvent(_message.Message):
    __slots__ = ("git_hash", "config")
    GIT_HASH_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    git_hash: str
    config: str
    def __init__(self, git_hash: _Optional[str] = ..., config: _Optional[str] = ...) -> None: ...

class Value(_message.Message):
    __slots__ = ("float_val", "int_val", "string_val", "bool_val")
    FLOAT_VAL_FIELD_NUMBER: _ClassVar[int]
    INT_VAL_FIELD_NUMBER: _ClassVar[int]
    STRING_VAL_FIELD_NUMBER: _ClassVar[int]
    BOOL_VAL_FIELD_NUMBER: _ClassVar[int]
    float_val: float
    int_val: int
    string_val: str
    bool_val: bool
    def __init__(self, float_val: _Optional[float] = ..., int_val: _Optional[int] = ..., string_val: _Optional[str] = ..., bool_val: bool = ...) -> None: ...

class ParamEvent(_message.Message):
    __slots__ = ("key", "value")
    KEY_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    key: str
    value: Value
    def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[Value, _Mapping]] = ...) -> None: ...

class MetricEvent(_message.Message):
    __slots__ = ("key", "value", "step")
    KEY_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    STEP_FIELD_NUMBER: _ClassVar[int]
    key: str
    value: float
    step: int
    def __init__(self, key: _Optional[str] = ..., value: _Optional[float] = ..., step: _Optional[int] = ...) -> None: ...

class ResultEvent(_message.Message):
    __slots__ = ("key", "value")
    KEY_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    key: str
    value: str
    def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

class ArtifactEvent(_message.Message):
    __slots__ = ("key", "local_path")
    KEY_FIELD_NUMBER: _ClassVar[int]
    LOCAL_PATH_FIELD_NUMBER: _ClassVar[int]
    key: str
    local_path: str
    def __init__(self, key: _Optional[str] = ..., local_path: _Optional[str] = ...) -> None: ...

class TrialEndEvent(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class Ack(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
