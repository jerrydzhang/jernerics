from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Task:
    name: str
    func: Callable[..., Any]
    depends_on: list[Task] = field(default_factory=list)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.func(*args, **kwargs)


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
        return task_instance

    if func is not None:
        return decorator(func)

    return decorator
