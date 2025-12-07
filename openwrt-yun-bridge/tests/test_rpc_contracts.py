"""Tests for automatically derived RPC contracts."""
from __future__ import annotations

from yunbridge.rpc import contracts
from yunbridge.rpc.protocol import Command


def test_expected_responses_infers_link_reset_pair() -> None:
    responses = contracts.expected_responses(Command.CMD_LINK_RESET.value)
    assert Command.CMD_LINK_RESET_RESP.value in responses


def test_response_to_request_lookup() -> None:
    request = contracts.response_to_request(
        Command.CMD_MAILBOX_AVAILABLE_RESP.value
    )
    assert request == Command.CMD_MAILBOX_AVAILABLE.value
