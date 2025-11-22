import pytest

from yunbridge.common import (
    DecodeError,
    chunk_payload,
    clamp,
    cobs_decode,
    cobs_encode,
    deduplicate,
    encode_status_reason,
    normalise_allowed_commands,
    pack_u16,
    unpack_u16,
)


def test_cobs_roundtrip():
    payload = b"hello\x00world"
    encoded = cobs_encode(payload)
    assert b"\x00" not in encoded
    decoded = cobs_decode(encoded)
    assert decoded == payload


@pytest.mark.parametrize(
    "invalid",
    [b"", b"\x01", b"\x02"],
)
def test_unpack_u16_invalid(invalid):
    with pytest.raises(ValueError):
        unpack_u16(invalid)


@pytest.mark.parametrize(
    "value,expected",
    [(0x1234, b"\x12\x34"), (0xFFFF, b"\xFF\xFF"), (0x100, b"\x01\x00")],
)
def test_pack_u16(value, expected):
    assert pack_u16(value) == expected


@pytest.mark.parametrize(
    "data,max_size,expected",
    [
        (b"", 8, tuple()),
        (b"abc", 2, (b"ab", b"c")),
        (b"abc", 3, (b"abc",)),
    ],
)
def test_chunk_payload(data, max_size, expected):
    assert chunk_payload(data, max_size) == expected


def test_chunk_payload_invalid():
    with pytest.raises(ValueError):
        chunk_payload(b"abc", 0)


@pytest.mark.parametrize(
    "value,minimum,maximum,expected",
    [(5, 0, 10, 5), (-1, 0, 10, 0), (15, 0, 10, 10)],
)
def test_clamp(value, minimum, maximum, expected):
    assert clamp(value, minimum, maximum) == expected


def test_normalise_allowed_commands_handles_wildcard():
    assert normalise_allowed_commands([" ls ", "*"]) == ("*",)


def test_normalise_allowed_commands():
    result = normalise_allowed_commands([" ls ", "LS", "echo", ""])
    assert result == ("ls", "echo")


def test_deduplicate_preserves_order():
    assert deduplicate(["a", "b", "a", "c"]) == ("a", "b", "c")


@pytest.mark.parametrize(
    "reason,expected",
    [
        (None, b""),
        ("", b""),
        ("ok", b"ok"),
    ],
)
def test_encode_status_reason_basic(reason, expected):
    assert encode_status_reason(reason) == expected


def test_encode_status_reason_truncates():
    reason = "x" * 300
    result = encode_status_reason(reason)
    assert len(result) == 256


def test_cobs_decode_error(monkeypatch):
    class FakeCodec:
        def decode(self, data):
            raise DecodeError("bad frame")

    from yunbridge import common as common_module

    monkeypatch.setattr(common_module, "_COBC_MODULE", FakeCodec())

    with pytest.raises(DecodeError):
        common_module.cobs_decode(b"123")
