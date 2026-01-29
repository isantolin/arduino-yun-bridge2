import random
import struct
import pytest
from cobs import cobs
from mcubridge.rpc.frame import Frame
from mcubridge.rpc.protocol import CRC_COVERED_HEADER_SIZE
from tests.test_constants import TEST_RANDOM_SEED

# Deterministic seed for reproducibility
FUZZ_ITERATIONS = 5000


@pytest.mark.fuzz
def test_frame_parsing_resilience_to_fuzzing():
    """Fuzzing test to ensure Frame.from_bytes never crashes with unhandled exceptions."""
    random.seed(TEST_RANDOM_SEED)

    valid_exceptions = (ValueError, struct.error, cobs.DecodeError)

    for i in range(FUZZ_ITERATIONS):
        # Generate random length between 0 and 200 bytes
        length = random.randint(0, 200)
        # Generate random bytes
        raw_data = random.randbytes(length)

        try:
            # We attempt to parse raw data directly as if it was decoded from COBS
            # (Testing the internal Frame structure parser)
            _ = Frame.from_bytes(raw_data)
        except valid_exceptions:
            # This is expected behavior for garbage data
            pass
        except Exception as exc:
            message = (
                f"Frame.from_bytes crashed on iteration {i} with unhandled exception: "
                f"{type(exc).__name__}: {exc}. Data hex: {raw_data.hex()}"
            )
            pytest.fail(message)


@pytest.mark.fuzz
def test_cobs_decoding_resilience():
    """Fuzzing test for COBS decoding wrapper."""
    random.seed(TEST_RANDOM_SEED)

    for i in range(FUZZ_ITERATIONS):
        length = random.randint(0, 200)
        raw_data = random.randbytes(length)

        try:
            # Most random data is invalid COBS (e.g. 0 byte in wrong place)
            _ = cobs.decode(raw_data)
        except cobs.DecodeError:
            pass
        except Exception as e:
            pytest.fail(f"cobs.decode crashed on iteration {i} with unhandled exception: {type(e).__name__}: {e}")


@pytest.mark.fuzz
def test_frame_header_parsing_resilience():
    """Specifically target the header parsing logic."""
    random.seed(TEST_RANDOM_SEED)

    for i in range(FUZZ_ITERATIONS):
        # Header is usually small, let's fuzz around that size
        length = random.randint(0, CRC_COVERED_HEADER_SIZE + 5)
        raw_data = random.randbytes(length)

        try:
            _ = Frame.from_bytes(raw_data)
        except (ValueError, struct.error):
            pass
        except Exception as e:
            pytest.fail(f"Header parsing crashed on iteration {i} with: {e}")
