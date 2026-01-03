from __future__ import annotations

from types import SimpleNamespace

from yunbridge.state import context
from yunbridge.rpc.protocol import Status


def test_command_name_formats_unknown_command_id() -> None:
    assert context._command_name(0xFF) == "0xFF"


def test_status_label_covers_none_and_unknown_code() -> None:
    assert context._status_label(None) == "unknown"
    assert context._status_label(0xFF) == "0xFF"


def test_status_label_returns_enum_name_for_known_status() -> None:
    assert context._status_label(Status.OK.value) == "OK"


def test_coerce_snapshot_int_handles_types_and_errors() -> None:
    snapshot = {
        "a": 12,
        "b": 12.7,
        "c": "42",
        "d": "not-an-int",
        "e": None,
    }

    assert context._coerce_snapshot_int(snapshot, "a", 0) == 12
    assert context._coerce_snapshot_int(snapshot, "b", 0) == 12
    assert context._coerce_snapshot_int(snapshot, "c", 0) == 42
    assert context._coerce_snapshot_int(snapshot, "d", 7) == 7
    assert context._coerce_snapshot_int(snapshot, "e", 9) == 9
    assert context._coerce_snapshot_int(snapshot, "missing", 11) == 11


def test_exponential_backoff_default_attempt_and_clamping() -> None:
    backoff = context._ExponentialBackoff(min_val=5.0, max_val=60.0, multiplier=5.0)

    # No attempt_number => default attempt 1
    assert backoff(SimpleNamespace()) == 5.0

    # Large attempt_number should clamp at max
    assert backoff(SimpleNamespace(attempt_number=999)) == 60.0


def test_append_with_limit_covers_empty_and_trim_paths() -> None:
    buf = bytearray(b"abc")

    # Empty chunk => no changes, not truncated
    assert context._append_with_limit(buf, b"", 3) is False
    assert bytes(buf) == b"abc"

    # Unlimited limit => never truncates
    assert context._append_with_limit(buf, b"def", 0) is False
    assert bytes(buf) == b"abcdef"

    # Enforced limit => truncates from the front
    assert context._append_with_limit(buf, b"ghij", 5) is True
    assert bytes(buf) == b"fghij"


def test_trim_process_buffers_drains_and_reports_truncation() -> None:
    stdout = bytearray(b"012345")
    stderr = bytearray(b"abcdef")

    out, err, trunc_out, trunc_err = context._trim_process_buffers(stdout, stderr, budget=5)

    assert out == b"01234"
    assert err == b""
    assert trunc_out is True
    assert trunc_err is True

    # Drain remaining
    out2, err2, trunc_out2, trunc_err2 = context._trim_process_buffers(stdout, stderr, budget=100)
    assert out2 == b"5"
    assert err2 == b"abcdef"
    assert trunc_out2 is False
    assert trunc_err2 is False
