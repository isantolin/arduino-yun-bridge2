"""Tests for automatically derived RPC contracts."""

from __future__ import annotations

from enum import IntEnum

import pytest
from mcubridge.protocol import contracts
from mcubridge.protocol.protocol import Command


def test_expected_responses_infers_link_reset_pair() -> None:
    responses = contracts.expected_responses(Command.CMD_LINK_RESET.value)
    assert Command.CMD_LINK_RESET_RESP.value in responses


def test_response_to_request_lookup() -> None:
    request = contracts.response_to_request(Command.CMD_MAILBOX_AVAILABLE_RESP.value)
    assert request == Command.CMD_MAILBOX_AVAILABLE.value


def test_request_response_map_skips_orphan_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCommand(IntEnum):
        CMD_OK = 1
        CMD_OK_RESP = 2
        CMD_ORPHAN_RESP = 3

    monkeypatch.setattr(contracts, "Command", FakeCommand)
    contracts.request_response_map.cache_clear()
    contracts.response_to_request_map.cache_clear()

    mapping = contracts.request_response_map()
    assert mapping == {1: frozenset({2})}

    reverse = contracts.response_to_request_map()
    assert reverse == {2: 1}

    contracts.request_response_map.cache_clear()
    contracts.response_to_request_map.cache_clear()
