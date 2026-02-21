"""Registry typing stub for prometheus_client."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol


class Collector(Protocol):
    def collect(self) -> Iterable[object]: ...

