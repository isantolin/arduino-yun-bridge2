"""Shared helpers derived from the generated RPC protocol metadata."""
from __future__ import annotations

from functools import lru_cache
from typing import Dict, FrozenSet, Optional

from .protocol import Command


@lru_cache(maxsize=1)
def request_response_map() -> Dict[int, FrozenSet[int]]:
    """Return a mapping of request command IDs to their response IDs."""

    pairs: Dict[int, set[int]] = {}
    for command in Command:
        name = command.name
        if not name.endswith("_RESP"):
            continue
        request_name = name[:-5]
        try:
            request = Command[request_name]
        except KeyError:
            continue
        pair = pairs.setdefault(request.value, set())
        pair.add(command.value)
    return {key: frozenset(value) for key, value in pairs.items()}


@lru_cache(maxsize=1)
def response_to_request_map() -> Dict[int, int]:
    """Return a mapping of response command IDs to their request IDs."""

    reverse: Dict[int, int] = {}
    for request_id, responses in request_response_map().items():
        for response_id in responses:
            reverse[response_id] = request_id
    return reverse


def expected_responses(command_id: int) -> FrozenSet[int]:
    """Look up the expected MCU responses for the given command ID."""

    return request_response_map().get(command_id, frozenset())


def response_to_request(command_id: int) -> Optional[int]:
    """Resolve the request ID for the given response command ID."""

    return response_to_request_map().get(command_id)
