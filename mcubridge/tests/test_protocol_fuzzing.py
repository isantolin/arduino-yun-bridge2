import random
from collections.abc import Callable

import pytest
from cobs import cobsr
from google.protobuf.message import DecodeError
from mcubridge.protocol.frame import parse_frame
from mcubridge.protocol.protocol import CRC_COVERED_HEADER_SIZE
from tests.test_constants import TEST_RANDOM_SEED

# Deterministic seed for reproducibility
FUZZ_ITERATIONS = 5000
EXPECTED_PARSE_ERRORS = (
    ValueError,
    DecodeError,
)
EXPECTED_COBS_ERRORS = (cobsr.DecodeError, ValueError)


def _assert_only_expected_exception(
    operation: Callable[[], object],
    expected: tuple[type[Exception], ...],
) -> bool:
    try:
        operation()
        return True
    except expected:
        return True


@pytest.mark.fuzz
def test_frame_parsing_resilience_to_fuzzing():
    """Fuzzing test to ensure parse_frame never crashes with unhandled exceptions."""
    random.seed(TEST_RANDOM_SEED)
    tested_count = 0

    for _ in range(FUZZ_ITERATIONS):
        # Generate random length between 0 and 200 bytes
        length = random.randint(0, 200)
        # Generate random bytes
        raw_data = random.randbytes(length)

        if _assert_only_expected_exception(
            lambda: parse_frame(raw_data),
            EXPECTED_PARSE_ERRORS,
        ):
            tested_count += 1
    assert tested_count == FUZZ_ITERATIONS


@pytest.mark.fuzz
def test_cobs_decoding_resilience():
    """Fuzzing test for COBS decoding wrapper."""
    random.seed(TEST_RANDOM_SEED)
    tested_count = 0

    for _ in range(FUZZ_ITERATIONS):
        length = random.randint(0, 200)
        raw_data = random.randbytes(length)

        if _assert_only_expected_exception(
            lambda: cobsr.decode(raw_data),
            EXPECTED_COBS_ERRORS,
        ):
            tested_count += 1
    assert tested_count == FUZZ_ITERATIONS


@pytest.mark.fuzz
def test_frame_header_parsing_resilience():
    """Specifically target the header parsing logic."""
    random.seed(TEST_RANDOM_SEED)
    tested_count = 0

    for _ in range(FUZZ_ITERATIONS):
        # Header is usually small, let's fuzz around that size
        length = random.randint(0, CRC_COVERED_HEADER_SIZE + 5)
        raw_data = random.randbytes(length)

        if _assert_only_expected_exception(
            lambda: parse_frame(raw_data),
            (ValueError, DecodeError),
        ):
            tested_count += 1
    assert tested_count == FUZZ_ITERATIONS
