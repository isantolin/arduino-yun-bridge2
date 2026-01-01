"""Centralized constants for testing purposes.
Do not use in production code.
"""
from typing import Final
from yunbridge.rpc import protocol

TEST_CMD_ID: Final[int] = 4660
TEST_MSG_ID: Final[int] = 4660
TEST_RANDOM_SEED: Final[int] = 3735928559
TEST_BROKEN_CRC: Final[int] = 305419896
TEST_BROKEN_ID: Final[int] = protocol.INVALID_ID_SENTINEL
TEST_PAYLOAD_BYTE: Final[int] = protocol.TEST_PAYLOAD_BYTE
TEST_MARKER_BYTE: Final[int] = protocol.TEST_MARKER_BYTE
