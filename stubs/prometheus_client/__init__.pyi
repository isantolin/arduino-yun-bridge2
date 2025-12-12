from typing import Any
from .registry import CollectorRegistry as CollectorRegistry

CONTENT_TYPE_LATEST: str

def generate_latest(registry: CollectorRegistry | None = ...) -> bytes: ...
