from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, overload

if TYPE_CHECKING:
    from .dag import DAG

_active_dag: ContextVar["DAG | None"] = ContextVar("_active_dag", default=None)

P = ParamSpec("P")
R = TypeVar("R")


@dataclass
class Task:
    name: str
    func: Callable[..., Any]
    depends_on: list["Task"] = field(default_factory=list)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.func(*args, **kwargs)


@overload
def task[**P, R](__func: Callable[P, R], /) -> Task: ...


@overload
def task(
    *,
    depends_on: list[Task] | None = None,
) -> Callable[[Callable[P, R]], Task]: ...


def task(
    func: Callable[..., Any] | None = None,
    *,
    depends_on: list[Task] | None = None,
) -> Task | Callable[[Callable[..., Any]], Task]:
    dependencies = depends_on or []

    def decorator(fn: Callable[..., Any]) -> Task:
        name = getattr(fn, "__name__", None) or getattr(type(fn), "__name__", repr(fn))
        task_instance = Task(
            name=name,
            func=fn,
            depends_on=list(dependencies),
        )
        task_instance.__doc__ = fn.__doc__
        task_instance.__module__ = fn.__module__

        dag = _active_dag.get()
        if dag is not None:
            dag.add_task(task_instance)

        return task_instance

    if func is not None:
        return decorator(func)

    return decorator
