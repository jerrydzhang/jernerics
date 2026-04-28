import contextlib
import inspect
import os
import traceback
import warnings
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from graphlib import TopologicalSorter
from typing import Any, Protocol, cast

from jernerics.tracking import NullTracker, Tracker

from .state import RunState, TaskStatus
from .task import Task


@dataclass
class TaskResult:
    value: Any = None
    error: Exception | None = None
    error_traceback: str | None = None

    @property
    def is_error(self) -> bool:
        return self.error is not None

    def __getitem__(self, key):
        return self.value[key]


class Handle(Protocol):
    def result(self) -> TaskResult: ...


class Runner(Protocol):
    def submit(
        self, fn: Callable[..., TaskResult], *args: Any, **kwargs: Any
    ) -> Handle: ...
    def collect(
        self, futures: dict[Handle, str]
    ) -> list[tuple[Handle, TaskResult]]: ...
    def shutdown(self) -> None: ...


class SyncHandle:
    def __init__(self, result: TaskResult) -> None:
        self._result = result

    def result(self) -> TaskResult:
        return self._result


class SyncRunner:
    def submit(
        self, fn: Callable[..., TaskResult], *args: Any, **kwargs: Any
    ) -> SyncHandle:
        return SyncHandle(fn(*args, **kwargs))

    def collect(self, futures: dict[Handle, str]) -> list[tuple[Handle, TaskResult]]:
        return [(handle, handle.result()) for handle in futures]

    def shutdown(self) -> None:
        pass


class ThreadPoolRunner:
    def __init__(self, max_workers: int | None) -> None:
        if max_workers is None:
            max_workers = _get_default_max_workers()
        if not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError(
                f"max_workers must be a positive integer, got {max_workers!r}"
            )
        self._executor = ThreadPoolExecutor(max_workers=max_workers)

    def submit(
        self, fn: Callable[..., TaskResult], *args: Any, **kwargs: Any
    ) -> Future[TaskResult]:
        return self._executor.submit(fn, *args, **kwargs)

    def collect(self, futures: dict[Handle, str]) -> list[tuple[Handle, TaskResult]]:
        done, _ = wait(
            cast(set[Future[TaskResult]], set(futures)),
            return_when=FIRST_COMPLETED,
        )
        return [(future, future.result()) for future in done]

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)


def _get_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_default_max_workers() -> int:
    return os.cpu_count() or 4


def execute_dag(
    tasks: dict[str, Task],
    config: dict[str, Any],
    tracker: Tracker | None = None,
    state: RunState | None = None,
    runner: Runner | None = None,
) -> dict[str, TaskResult]:
    if runner is None:
        runner = ThreadPoolRunner(max_workers=None)

    graph: dict[str, set[str]] = {}
    for task_name, task in tasks.items():
        dep_names = set()
        for dep in task.depends_on:
            if dep.name in tasks:
                dep_names.add(dep.name)
            else:
                raise ValueError(
                    f"Task '{task_name}' depends on unknown task '{dep.name}'."
                )
        graph[task_name] = dep_names

    # retry failed tasks
    if state:
        for task_name, task_state in state.tasks.items():
            if task_state.status == TaskStatus.FAILED:
                state.update_task(task_name, TaskStatus.PENDING)

    sorter = TopologicalSorter(graph)
    sorter.prepare()

    futures: dict[Handle, str] = {}
    results: dict[str, TaskResult] = {}

    try:
        while sorter.is_active():
            ready_task_names = sorter.get_ready()

            if not ready_task_names and not futures:
                break

            for task_name in ready_task_names:
                task = tasks[task_name]

                # state persistence
                if state and task_name in state.tasks:
                    task_state = state.tasks[task_name]
                    if (
                        task_state.status == TaskStatus.COMPLETED
                        and task_state.persisted
                    ):
                        results[task_name] = TaskResult(value=task_state.output)
                        sorter.done(task_name)
                        continue

                # check upstream failures
                if any(results[dep.name].is_error for dep in task.depends_on):
                    error_msg = (
                        f"Task '{task_name}' cannot run because"
                        " one or more dependencies failed."
                    )
                    results[task_name] = TaskResult(error=Exception(error_msg))

                    if state:
                        state.update_task(
                            task_name,
                            TaskStatus.FAILED,
                            error=error_msg,
                        )
                        state.to_json()

                    sorter.done(task_name)

                    continue

                inputs = {
                    dep.name: results[dep.name].value
                    for dep in task.depends_on
                    if not results[dep.name].is_error
                }

                if state:
                    state.update_task(
                        task_name,
                        TaskStatus.RUNNING,
                        started_at=_get_timestamp(),
                    )
                    state.to_json()

                future = runner.submit(_run_task, task, inputs, config, tracker)
                futures[future] = task_name

            if not futures:
                continue

            for future, task_result in runner.collect(futures):
                task_name = futures.pop(future)

                results[task_name] = task_result

                if state:
                    state.update_task(
                        task_name,
                        (
                            TaskStatus.COMPLETED
                            if not task_result.is_error
                            else TaskStatus.FAILED
                        ),
                        completed_at=_get_timestamp(),
                        output=(
                            task_result.value if not task_result.is_error else None
                        ),
                        error=(
                            task_result.error_traceback
                            if task_result.is_error
                            else None
                        ),
                    )
                    state.to_json()

                sorter.done(task_name)

    except Exception:
        with contextlib.suppress(Exception):
            for task_name in sorter.get_ready():
                sorter.done(task_name)
        raise

    return results


def _run_task(
    task: Task,
    inputs: dict[str, Any],
    config: dict[str, Any],
    tracker: Tracker | None,
) -> TaskResult:
    sig = inspect.signature(task.func)
    if "config" in inputs and "config" in sig.parameters:
        warnings.warn(
            f"Task '{task.name}' has a parameter named 'config' which will be "
            f"overwritten by the DAG config dict. Consider renaming the parameter.",
            UserWarning,
            stacklevel=2,
        )
    if "tracker" in inputs and "tracker" in sig.parameters:
        warnings.warn(
            f"Task '{task.name}' has a parameter named 'tracker' which will be "
            f"overwritten by the DAG tracker. Consider renaming the parameter.",
            UserWarning,
            stacklevel=2,
        )

    tracker = tracker or NullTracker()
    kwargs = {**inputs, "config": config}
    if "tracker" in sig.parameters:
        kwargs["tracker"] = tracker

    try:
        result = task.func(**kwargs)
        return TaskResult(value=result)
    except Exception as e:
        tb_str = "".join(
            traceback.format_exception(
                type(e),
                e,
                e.__traceback__,
            )
        )
        return TaskResult(error=e, error_traceback=tb_str)
