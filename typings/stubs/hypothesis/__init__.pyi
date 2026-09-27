"""Type stub for hypothesis package [SIL-2]."""

from collections.abc import Callable, Iterable
from enum import Enum
from typing import Any, TypeVar

from . import stateful as stateful, strategies as strategies

_T = TypeVar("_T")
_Test = TypeVar("_Test", bound=Callable[..., Any])

class HealthCheck(Enum):
    too_slow = 1
    filter_too_much = 2

def event(value: str) -> None: ...
def seed(seed: int | None) -> Callable[[_T], _T]: ...

class settings:
    def __init__(
        self,
        parent: settings | None = None,
        max_examples: int = ...,
        derandomize: bool = ...,
        database: Any = ...,
        verbosity: Any = ...,
        phases: Any = ...,
        stateful_step_count: int = ...,
        report_multiple_bugs: bool = ...,
        suppress_health_check: Iterable[HealthCheck] = ...,
        deadline: float | None = ...,
        print_blob: bool = ...,
    ) -> None: ...
    @classmethod
    def register_profile(cls, name: str, parent_settings: settings | None = None, **kwargs: Any) -> None: ...
    @classmethod
    def load_profile(cls, name: str) -> None: ...
    def __call__(self, test: _Test) -> _Test: ...

__all__ = ["HealthCheck", "event", "seed", "settings", "stateful", "strategies"]
