import pytest

from yunbridge.common import (
    chunk_payload,
    clamp,
    deduplicate,
    encode_status_reason,
    normalise_allowed_commands,
    pack_u16,
    unpack_u16,
)


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
