from __future__ import annotations

import json

import numpy as np
from hypothesis import given
from hypothesis import strategies as st

from jernerics.dag.state import (
    RunState,
    TaskState,
    TaskStatus,
    _deserialize_output,
    _serialize_output,
)


class TestSerialization:
    def test_serialize_json_compatible(self):
        output = {"result": 42, "name": "test"}
        serialized, persisted = _serialize_output(output)
        assert persisted is True
        assert serialized == output

    def test_serialize_numpy_array(self):
        output = np.array([1, 2, 3])
        serialized, persisted = _serialize_output(output)
        assert persisted is True
        assert isinstance(serialized, dict)
        assert serialized.get("__jernerics_serialized__") == "cloudpickle"

    def test_serialize_non_serializable(self):
        import socket

        output = socket.socket()
        try:
            serialized, persisted = _serialize_output(output)
            assert persisted is False
            assert "non-serializable" in serialized
        finally:
            output.close()

    def test_serialize_none(self):
        serialized, persisted = _serialize_output(None)
        assert persisted is True
        assert serialized is None

    def test_roundtrip_numpy_array(self):
        output = np.array([[1.0, 2.0], [3.0, 4.0]])
        serialized, _ = _serialize_output(output)
        deserialized = _deserialize_output(serialized)
        assert isinstance(deserialized, np.ndarray)
        assert np.array_equal(deserialized, output)

    def test_roundtrip_json_output(self):
        output = {"values": [1, 2, 3], "nested": {"a": "b"}}
        serialized, _ = _serialize_output(output)
        deserialized = _deserialize_output(serialized)
        assert deserialized == output

    def test_task_state_numpy_output_roundtrip(self):
        state = TaskState(
            task_id="test",
            status=TaskStatus.COMPLETED,
            output=np.array([1, 2, 3]),
        )
        d = state.to_dict()
        assert d["persisted"] is True
        restored = TaskState.from_dict(d)
        assert isinstance(restored.output, np.ndarray)
        assert np.array_equal(restored.output, np.array([1, 2, 3]))

    def test_task_state_non_serializable_output(self):
        import socket

        sock = socket.socket()
        try:
            state = TaskState(
                task_id="test",
                status=TaskStatus.COMPLETED,
                output=sock,
            )
            d = state.to_dict()
            assert d["persisted"] is False
            assert "non-serializable" in d["output"]
            restored = TaskState.from_dict(d)
            assert restored.persisted is False
        finally:
            sock.close()


class TestTaskStatus:
    def test_task_status_is_string_enum(self):
        assert TaskStatus.PENDING.value == "pending"
        assert TaskStatus.RUNNING.value == "running"
        assert TaskStatus.COMPLETED.value == "completed"
        assert TaskStatus.FAILED.value == "failed"

    def test_task_status_string_comparison(self):
        assert TaskStatus.PENDING == "pending"
        assert TaskStatus.RUNNING == "running"
        assert TaskStatus.COMPLETED == "completed"
        assert TaskStatus.FAILED == "failed"

    @given(st.sampled_from(TaskStatus))
    def test_task_status_roundtrip(self, status):
        assert TaskStatus(status.value) == status


class TestTaskState:
    def test_task_state_creation(self):
        state = TaskState(task_id="my_task", status=TaskStatus.PENDING)
        assert state.task_id == "my_task"
        assert state.status == TaskStatus.PENDING
        assert state.started_at is None
        assert state.completed_at is None
        assert state.output is None
        assert state.error is None

    def test_task_state_to_dict(self):
        state = TaskState(
            task_id="my_task",
            status=TaskStatus.COMPLETED,
            started_at="2024-01-01T00:00:00Z",
            completed_at="2024-01-01T00:01:00Z",
            output={"result": 42},
            error=None,
        )
        d = state.to_dict()

        assert d["task_id"] == "my_task"
        assert d["status"] == "completed"
        assert d["started_at"] == "2024-01-01T00:00:00Z"
        assert d["completed_at"] == "2024-01-01T00:01:00Z"
        assert d["output"] == {"result": 42}
        assert d["error"] is None

    def test_task_state_from_dict(self):
        d = {
            "task_id": "my_task",
            "status": "failed",
            "started_at": "2024-01-01T00:00:00Z",
            "completed_at": "2024-01-01T00:01:00Z",
            "output": None,
            "error": "Something went wrong",
        }
        state = TaskState.from_dict(d)

        assert state.task_id == "my_task"
        assert state.status == TaskStatus.FAILED
        assert state.started_at == "2024-01-01T00:00:00Z"
        assert state.completed_at == "2024-01-01T00:01:00Z"
        assert state.output is None
        assert state.error == "Something went wrong"

    @given(
        task_id=st.text(
            min_size=1,
            max_size=50,
            alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
        ),
        status=st.sampled_from(TaskStatus),
    )
    def test_task_state_roundtrip(self, task_id, status):
        state = TaskState(task_id=task_id, status=status)
        d = state.to_dict()
        restored = TaskState.from_dict(d)

        assert restored.task_id == task_id
        assert restored.status == status

    @given(
        output=st.one_of(
            st.none(),
            st.integers(),
            st.text(),
            st.lists(st.integers()),
            st.dictionaries(st.text(), st.integers()),
        )
    )
    def test_task_state_various_outputs(self, output):
        state = TaskState(task_id="test", status=TaskStatus.COMPLETED, output=output)
        d = state.to_dict()
        restored = TaskState.from_dict(d)

        assert restored.output == output


class TestRunState:
    def test_run_state_creation(self, tmp_path):
        state = RunState.create(
            dag_file="test_dag.py",
            config_index=0,
            state_dir=tmp_path,
        )

        assert state.run_id is not None
        assert state.created_at is not None
        assert state.dag_file == "test_dag.py"
        assert state.config_index == 0
        assert state.tasks == {}

    def test_run_state_init_task(self, tmp_path):
        state = RunState.create(
            dag_file="test_dag.py",
            config_index=0,
            state_dir=tmp_path,
        )

        state.init_task("task_a")
        state.init_task("task_b")

        assert "task_a" in state.tasks
        assert state.tasks["task_a"].status == TaskStatus.PENDING
        assert "task_b" in state.tasks
        assert state.tasks["task_b"].status == TaskStatus.PENDING

    def test_run_state_init_task_idempotent(self, tmp_path):
        state = RunState.create(
            dag_file="test_dag.py",
            config_index=0,
            state_dir=tmp_path,
        )

        state.init_task("task_a")
        state.tasks["task_a"].status = TaskStatus.RUNNING

        state.init_task("task_a")

        assert state.tasks["task_a"].status == TaskStatus.RUNNING

    def test_run_state_update_task(self, tmp_path):
        state = RunState.create(
            dag_file="test_dag.py",
            config_index=0,
            state_dir=tmp_path,
        )

        state.init_task("task_a")
        state.update_task(
            "task_a",
            TaskStatus.COMPLETED,
            started_at="2024-01-01T00:00:00Z",
            completed_at="2024-01-01T00:01:00Z",
            output=42,
        )

        assert state.tasks["task_a"].status == TaskStatus.COMPLETED
        assert state.tasks["task_a"].output == 42

    def test_run_state_update_nonexistent_task(self, tmp_path):
        state = RunState.create(
            dag_file="test_dag.py",
            config_index=0,
            state_dir=tmp_path,
        )

        state.update_task("new_task", TaskStatus.RUNNING)

        assert "new_task" in state.tasks
        assert state.tasks["new_task"].status == TaskStatus.RUNNING

    def test_run_state_to_json(self, tmp_path):
        state = RunState.create(
            dag_file="test_dag.py",
            config_index=0,
            state_dir=tmp_path,
        )

        state.init_task("task_a")
        state.update_task("task_a", TaskStatus.COMPLETED, output="done")

        path = state.to_json()

        assert path.exists()
        with open(path) as f:
            data = json.load(f)

        assert data["dag_file"] == "test_dag.py"
        assert data["config_index"] == 0
        assert "task_a" in data["tasks"]
        assert data["tasks"]["task_a"]["status"] == "completed"

    def test_run_state_from_json(self, tmp_path):
        original = RunState.create(
            dag_file="test_dag.py",
            config_index=1,
            state_dir=tmp_path,
        )
        original.init_task("task_a")
        original.update_task("task_a", TaskStatus.FAILED, error="boom")

        path = original.to_json()
        restored = RunState.from_json(path)

        assert restored.run_id == original.run_id
        assert restored.dag_file == "test_dag.py"
        assert restored.config_index == 1
        assert "task_a" in restored.tasks
        assert restored.tasks["task_a"].status == TaskStatus.FAILED
        assert restored.tasks["task_a"].error == "boom"

    def test_run_state_from_json_backward_compat(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir(parents=True)
        state_file = runs_dir / "20240101-120000-000000_0.json"
        state_file.write_text(
            '{"run_id": "20240101-120000-000000", '
            '"created_at": "2024-01-01T12:00:00Z", '
            '"dag_file": "test.py", "config_index": 0, "tasks": {}}'
        )

        restored = RunState.from_json(state_file)

        assert restored.run_id == "20240101-120000-000000"
        assert restored.state_dir == tmp_path

    def test_run_state_latest_file_created(self, tmp_path):
        state = RunState.create(
            dag_file="test_dag.py",
            config_index=0,
            state_dir=tmp_path,
        )
        state.init_task("task_a")

        state.to_json()

        latest = tmp_path / "runs" / "latest_0.json"
        assert latest.exists()

    def test_run_state_get_latest_run(self, tmp_path):
        state1 = RunState.create(
            dag_file="test_dag.py",
            config_index=0,
            state_dir=tmp_path,
        )
        state1.init_task("task_a")
        state1.to_json()

        state2 = RunState.create(
            dag_file="test_dag.py",
            config_index=0,
            state_dir=tmp_path,
        )
        state2.init_task("task_b")
        state2.to_json()

        latest = RunState.get_latest_run(tmp_path, 0)

        assert latest is not None
        assert latest.run_id == state2.run_id

    def test_run_state_get_latest_run_no_runs(self, tmp_path):
        result = RunState.get_latest_run(tmp_path, 0)
        assert result is None

    def test_run_state_different_config_indices(self, tmp_path):
        state0 = RunState.create(
            dag_file="test_dag.py",
            config_index=0,
            state_dir=tmp_path,
        )
        state1 = RunState.create(
            dag_file="test_dag.py",
            config_index=1,
            state_dir=tmp_path,
        )

        path0 = state0.to_json()
        path1 = state1.to_json()

        assert "_0.json" in str(path0)
        assert "_1.json" in str(path1)

    def test_run_state_multiple_tasks(self, tmp_path):
        state = RunState.create(
            dag_file="test_dag.py",
            config_index=0,
            state_dir=tmp_path,
        )

        for i in range(5):
            state.init_task(f"task_{i}")

        path = state.to_json()
        restored = RunState.from_json(path)

        assert len(restored.tasks) == 5
        for i in range(5):
            assert f"task_{i}" in restored.tasks
