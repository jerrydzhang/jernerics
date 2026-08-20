"""The narrow search-space surface sweep configs program against.

``SweepConfig.search_space`` receives a trial satisfying this protocol; an
optuna ``Trial`` satisfies it structurally, so configs stay optimizer-agnostic
without an adapter class or a plugin registry.
"""

from typing import Any, Protocol


class SearchTrial(Protocol):
    def suggest_float(
        self,
        name: str,
        low: float,
        high: float,
        *,
        log: bool = False,
        step: float | None = None,
    ) -> float: ...

    def suggest_int(
        self, name: str, low: int, high: int, *, step: int = 1, log: bool = False
    ) -> int: ...

    def suggest_categorical(self, name: str, choices: list[Any]) -> Any: ...
