import random
from collections.abc import Callable

import pytest
from cobs import cobs
from mcubridge.protocol.frame import parse_frame
from mcubridge.protocol.protocol import CRC_COVERED_HEADER_SIZE
from tests.test_constants import TEST_RANDOM_SEED

# Deterministic seed for reproducibility
FUZZ_ITERATIONS = 5000
EXPECTED_PARSE_ERRORS = (
    ValueError,
    TypeError,
    LookupError,
    RuntimeError,
    AttributeError,
)
EXPECTED_COBS_ERRORS = (cobs.DecodeError, ValueError, TypeError)


def _assert_only_expected_exception(
    operation: Callable[[], object],
    expected: tuple[type[Exception], ...],
    *,
    iteration: int,
    label: str,
    raw_data: bytes,
) -> None:
    try:
        operation()
    except expected as exc:
        assert isinstance(exc, expected)
    except Exception as exc:
        pytest.fail(
            f"{label} crashed on iteration {iteration} with unhandled exception: "
            f"{type(exc).__name__}: {exc}. Data hex: {raw_data.hex()}"
        )


@pytest.mark.fuzz
def test_frame_parsing_resilience_to_fuzzing():
    """Fuzzing test to ensure parse_frame never crashes with unhandled exceptions."""
    random.seed(TEST_RANDOM_SEED)

    for i in range(FUZZ_ITERATIONS):
        # Generate random length between 0 and 200 bytes
        length = random.randint(0, 200)
        # Generate random bytes
        raw_data = random.randbytes(length)

        _assert_only_expected_exception(
            lambda: parse_frame(raw_data),
            EXPECTED_PARSE_ERRORS,
            iteration=i,
            label="parse_frame",
            raw_data=raw_data,
        )


@pytest.mark.fuzz
def test_cobs_decoding_resilience():
    """Fuzzing test for COBS decoding wrapper."""
    random.seed(TEST_RANDOM_SEED)

    for i in range(FUZZ_ITERATIONS):
        length = random.randint(0, 200)
        raw_data = random.randbytes(length)

        _assert_only_expected_exception(
            lambda: cobs.decode(raw_data),
            EXPECTED_COBS_ERRORS,
            iteration=i,
            label="cobs.decode",
            raw_data=raw_data,
        )


@pytest.mark.fuzz
def test_frame_header_parsing_resilience():
    """Specifically target the header parsing logic."""
    random.seed(TEST_RANDOM_SEED)

    for i in range(FUZZ_ITERATIONS):
        # Header is usually small, let's fuzz around that size
        length = random.randint(0, CRC_COVERED_HEADER_SIZE + 5)
        raw_data = random.randbytes(length)

        _assert_only_expected_exception(
            lambda: parse_frame(raw_data),
            (ValueError, TypeError, LookupError),
            iteration=i,
            label="header parsing",
            raw_data=raw_data,
        )
