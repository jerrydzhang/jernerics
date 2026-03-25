from __future__ import annotations

import concurrent.futures
import contextlib
import inspect
import os
import traceback
import warnings
from concurrent.futures import ALL_COMPLETED
from datetime import datetime, timezone
from graphlib import TopologicalSorter
from typing import Any

from .state import RunState, TaskStatus
from .task import Task


def _get_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_default_max_workers() -> int:
    return os.cpu_count() or 4


def execute_dag(
    tasks: dict[str, Task],
    config: dict[str, Any],
    state: RunState | None = None,
    max_workers: int | None = None,
    executor_type: str = "thread",
) -> dict[str, Any]:
    if executor_type not in ("thread", "serial"):
        raise ValueError(
            f"executor_type must be 'thread' or 'serial', got {executor_type!r}"
        )
    if max_workers is None:
        max_workers = _get_default_max_workers()
    if not isinstance(max_workers, int) or max_workers < 1:
        raise ValueError(f"max_workers must be a positive integer, got {max_workers!r}")
    graph: dict[str, set[str]] = {}
    for task_name, task in tasks.items():
        dep_names = set()
        for dep in task.depends_on:
            if dep.name in tasks:
                dep_names.add(dep.name)
            else:
                warnings.warn(
                    f"Task '{task_name}' depends on unregistered task '{dep.name}'. "
                    f"This dependency will be ignored.",
                    UserWarning,
                    stacklevel=2,
                )
        graph[task_name] = dep_names

    results: dict[str, Any] = {}
    failed_tasks: set[str] = set()

    if state:
        for task_name, task_state in state.tasks.items():
            if task_state.status == TaskStatus.FAILED:
                state.update_task(task_name, TaskStatus.PENDING)

    sorter = TopologicalSorter(graph)
    sorter.prepare()

    try:
        if executor_type == "serial":
            while sorter.is_active():
                ready_tasks = sorter.get_ready()

                if not ready_tasks:
                    break

                for task_name in ready_tasks:
                    task = tasks[task_name]

                    if state and task_name in state.tasks:
                        task_state = state.tasks[task_name]
                        if (
                            task_state.status == TaskStatus.COMPLETED
                            and task_state.persisted
                        ):
                            results[task_name] = task_state.output
                            sorter.done(task_name)
                            continue

                    upstream_failed = any(
                        dep.name in failed_tasks for dep in task.depends_on
                    )

                    if upstream_failed:
                        results[task_name] = Exception(
                            f"Upstream task(s) failed for '{task_name}'"
                        )
                        failed_tasks.add(task_name)
                        sorter.done(task_name)

                        if state:
                            state.update_task(
                                task_name,
                                TaskStatus.FAILED,
                                error="Upstream task(s) failed",
                            )
                            state.to_json()
                    else:
                        inputs: dict[str, Any] = {}
                        for dep in task.depends_on:
                            if dep.name in results:
                                dep_result = results[dep.name]
                                if not isinstance(dep_result, Exception):
                                    inputs[dep.name] = dep_result

                        if state:
                            state.update_task(
                                task_name,
                                TaskStatus.RUNNING,
                                started_at=_get_timestamp(),
                            )
                            state.to_json()

                        try:
                            result = _run_task(task, inputs, config)
                            results[task_name] = result

                            if state:
                                state.update_task(
                                    task_name,
                                    TaskStatus.COMPLETED,
                                    completed_at=_get_timestamp(),
                                    output=result,
                                )
                                state.to_json()
                        except Exception as e:
                            tb_str = "".join(
                                traceback.format_exception(type(e), e, e.__traceback__)
                            )
                            results[task_name] = e
                            failed_tasks.add(task_name)

                            if state:
                                state.update_task(
                                    task_name,
                                    TaskStatus.FAILED,
                                    completed_at=_get_timestamp(),
                                    error=f"{type(e).__name__}: {e}\n{tb_str}",
                                )
                                state.to_json()

                        finally:
                            sorter.done(task_name)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                while sorter.is_active():
                    ready_tasks = sorter.get_ready()

                    if not ready_tasks:
                        break

                    futures: dict[concurrent.futures.Future[Any], str] = {}
                    for task_name in ready_tasks:
                        task = tasks[task_name]

                        if state and task_name in state.tasks:
                            task_state = state.tasks[task_name]
                            if (
                                task_state.status == TaskStatus.COMPLETED
                                and task_state.persisted
                            ):
                                results[task_name] = task_state.output
                                sorter.done(task_name)
                                continue

                        upstream_failed = any(
                            dep.name in failed_tasks for dep in task.depends_on
                        )

                        if upstream_failed:
                            results[task_name] = Exception(
                                f"Upstream task(s) failed for '{task_name}'"
                            )
                            failed_tasks.add(task_name)
                            sorter.done(task_name)

                            if state:
                                state.update_task(
                                    task_name,
                                    TaskStatus.FAILED,
                                    error="Upstream task(s) failed",
                                )
                                state.to_json()
                        else:
                            inputs: dict[str, Any] = {}
                            for dep in task.depends_on:
                                if dep.name in results:
                                    dep_result = results[dep.name]
                                    if not isinstance(dep_result, Exception):
                                        inputs[dep.name] = dep_result

                            if state:
                                state.update_task(
                                    task_name,
                                    TaskStatus.RUNNING,
                                    started_at=_get_timestamp(),
                                )
                                state.to_json()

                            future = pool.submit(_run_task, task, inputs, config)
                            futures[future] = task_name

                    if futures:
                        completed, _ = concurrent.futures.wait(
                            futures, return_when=ALL_COMPLETED
                        )

                        for future in completed:
                            task_name = futures[future]
                            try:
                                result = future.result()
                                results[task_name] = result

                                if state:
                                    state.update_task(
                                        task_name,
                                        TaskStatus.COMPLETED,
                                        completed_at=_get_timestamp(),
                                        output=result,
                                    )
                                    state.to_json()
                            except Exception as e:
                                tb_str = "".join(
                                    traceback.format_exception(
                                        type(e), e, e.__traceback__
                                    )
                                )
                                results[task_name] = e
                                failed_tasks.add(task_name)

                                if state:
                                    state.update_task(
                                        task_name,
                                        TaskStatus.FAILED,
                                        completed_at=_get_timestamp(),
                                        error=f"{type(e).__name__}: {e}\n{tb_str}",
                                    )
                                    state.to_json()

                            finally:
                                sorter.done(task_name)
    except Exception:
        with contextlib.suppress(Exception):
            for task_name in sorter.get_ready():
                sorter.done(task_name)
        raise

    return results


def _run_task(task: Task, inputs: dict[str, Any], config: dict[str, Any]) -> Any:
    sig = inspect.signature(task.func)
    if "config" in inputs and "config" in sig.parameters:
        warnings.warn(
            f"Task '{task.name}' has a parameter named 'config' which will be "
            f"overwritten by the DAG config dict. Consider renaming the parameter.",
            UserWarning,
            stacklevel=2,
        )
    kwargs = {**inputs, "config": config}
    return task.func(**kwargs)
