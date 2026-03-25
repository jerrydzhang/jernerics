from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

_SERIALIZATION_KEY = "__jernerics_serialized__"
_UNSET = object()


def _serialize_output(output: Any) -> tuple[Any, bool]:
    if output is None:
        return None, True
    try:
        json.dumps(output)
        return output, True
    except (TypeError, ValueError):
        try:
            import cloudpickle

            return {
                _SERIALIZATION_KEY: "cloudpickle",
                "data": cloudpickle.dumps(output).hex(),
            }, True
        except Exception:
            return f"<non-serializable: {type(output).__name__}>", False


class DeserializationError(Exception):
    pass


def _deserialize_output(data: Any) -> Any:
    if isinstance(data, dict) and data.get(_SERIALIZATION_KEY) == "cloudpickle":
        try:
            import cloudpickle

            return cloudpickle.loads(bytes.fromhex(data["data"]))
        except (KeyError, ValueError, Exception) as e:
            raise DeserializationError(
                f"Failed to deserialize cloudpickle output: {e}"
            ) from e
    return data


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TaskState:
    task_id: str
    status: TaskStatus
    started_at: str | None = None
    completed_at: str | None = None
    output: Any = None
    persisted: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        serialized_output, persisted = _serialize_output(self.output)
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "output": serialized_output,
            "persisted": persisted,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskState:
        output_data = data.get("output")
        persisted = data.get("persisted", True)
        try:
            output = _deserialize_output(output_data)
        except DeserializationError as e:
            _logger.warning(
                "Failed to deserialize output for task %s: %s",
                data.get("task_id", "unknown"),
                e,
            )
            output = None
            persisted = False
        return cls(
            task_id=data["task_id"],
            status=TaskStatus(data["status"]),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            output=output,
            persisted=persisted,
            error=data.get("error"),
        )


@dataclass
class RunState:
    run_id: str
    created_at: str
    dag_file: str
    config_index: int
    tasks: dict[str, TaskState] = field(default_factory=dict)
    state_dir: Path = field(default_factory=lambda: Path(".jernerics"))

    @staticmethod
    def _generate_run_id() -> str:
        return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")

    @staticmethod
    def _get_timestamp() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    @classmethod
    def create(cls, dag_file: str, config_index: int, state_dir: Path) -> RunState:
        return cls(
            run_id=cls._generate_run_id(),
            created_at=cls._get_timestamp(),
            dag_file=dag_file,
            config_index=config_index,
            tasks={},
            state_dir=state_dir,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "dag_file": self.dag_file,
            "config_index": self.config_index,
            "state_dir": str(self.state_dir),
            "tasks": {name: state.to_dict() for name, state in self.tasks.items()},
        }

    def to_json(self) -> Path:
        runs_dir = self.state_dir / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{self.run_id}_{self.config_index}.json"
        state_file = runs_dir / filename
        data = self.to_dict()

        temp_state = runs_dir / f".tmp_{filename}"
        with open(temp_state, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(temp_state, state_file)

        latest_file = runs_dir / f"latest_{self.config_index}.json"
        temp_latest = runs_dir / f".tmp_latest_{self.config_index}.json"
        with open(temp_latest, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(temp_latest, latest_file)

        return state_file

    @classmethod
    def from_json(cls, path: Path) -> RunState:
        with open(path) as f:
            data = json.load(f)

        tasks = {
            name: TaskState.from_dict(state_data)
            for name, state_data in data.get("tasks", {}).items()
        }

        return cls(
            run_id=data["run_id"],
            created_at=data["created_at"],
            dag_file=data["dag_file"],
            config_index=data["config_index"],
            tasks=tasks,
            state_dir=Path(data["state_dir"])
            if "state_dir" in data
            else path.parent.parent,
        )

    @classmethod
    def get_latest_run(cls, state_dir: Path, config_index: int) -> RunState | None:
        runs_dir = state_dir / "runs"
        latest_file = runs_dir / f"latest_{config_index}.json"
        if latest_file.exists():
            return cls.from_json(latest_file)

        run_files = sorted(
            [
                f
                for f in runs_dir.glob(f"*_{config_index}.json")
                if "latest" not in f.name
            ],
            reverse=True,
        )
        if not run_files:
            return None

        return cls.from_json(run_files[0])

    def update_task(
        self,
        task_id: str,
        status: TaskStatus,
        started_at: str | None = None,
        completed_at: str | None = None,
        output: Any = _UNSET,
        error: str | None = None,
    ) -> None:
        if task_id in self.tasks:
            task_state = self.tasks[task_id]
            task_state.status = status
            if started_at is not None:
                task_state.started_at = started_at
            if completed_at is not None:
                task_state.completed_at = completed_at
            if output is not _UNSET:
                task_state.output = output
            if error is not None:
                task_state.error = error
        else:
            self.tasks[task_id] = TaskState(
                task_id=task_id,
                status=status,
                started_at=started_at,
                completed_at=completed_at,
                output=None if output is _UNSET else output,
                error=error,
            )

    def init_task(self, task_id: str) -> None:
        if task_id not in self.tasks:
            self.tasks[task_id] = TaskState(task_id=task_id, status=TaskStatus.PENDING)
