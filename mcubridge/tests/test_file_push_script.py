"""Unit tests for mcubridge_file_push script (SIL-2)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Callable, cast
from unittest.mock import MagicMock

import pytest

# Dynamically load the standalone script
_script_path = Path(__file__).resolve().parent.parent / "scripts" / "mcubridge_file_push.py"
_spec = importlib.util.spec_from_file_location("mcubridge_file_push", str(_script_path))
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load mcubridge_file_push.py")
_file_push: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_file_push)

push_file = cast(Callable[..., None], getattr(_file_push, "push_file"))
push_file_ubus = cast(Callable[..., bool], getattr(_file_push, "push_file_ubus"))
cli_main = cast(Callable[..., None], getattr(_file_push, "main"))


def test_push_file_ubus_success() -> None:
    mock_ubus: Any = MagicMock()
    mock_conn: Any = MagicMock()
    mock_conn.call.return_value = {"status": "ok", "path": "test.txt", "bytes_written": 5}
    mock_ubus.connect.return_value = mock_conn

    res = push_file_ubus("test.txt", b"hello", ubus_module=mock_ubus)
    assert res is True
    assert mock_conn.call.called
    call_args = mock_conn.call.call_args[0]
    assert call_args[0] == "mcubridge"
    assert call_args[1] == "file_write"
    assert call_args[2] == {"path": "test.txt", "data": "hello"}


def test_push_file_ubus_binary_hex_fallback() -> None:
    mock_ubus: Any = MagicMock()
    mock_conn: Any = MagicMock()
    mock_conn.call.return_value = {"status": "ok", "path": "bin.dat", "bytes_written": 3}
    mock_ubus.connect.return_value = mock_conn

    res = push_file_ubus("bin.dat", b"\xff\xfe\x00", ubus_module=mock_ubus)
    assert res is True
    call_args = mock_conn.call.call_args[0]
    assert call_args[2]["data"] == "fffe00"


def test_push_file_ubus_failure_returns_false() -> None:
    mock_ubus: Any = MagicMock()
    mock_conn: Any = MagicMock()
    mock_conn.call.return_value = {"status": "error"}
    mock_ubus.connect.return_value = mock_conn

    assert push_file_ubus("test.txt", b"data", ubus_module=mock_ubus) is False

    # Connection failure
    mock_ubus.connect.return_value = None
    assert push_file_ubus("test.txt", b"data", ubus_module=mock_ubus) is False

    # Exception
    mock_ubus.connect.side_effect = OSError("Connection refused")
    assert push_file_ubus("test.txt", b"data", ubus_module=mock_ubus) is False


def test_push_file_direct_ubus() -> None:
    # 1. When UBUS succeeds
    mock_ubus_ok = MagicMock()
    mock_conn = MagicMock()
    mock_conn.call.return_value = {"status": "ok"}
    mock_ubus_ok.connect.return_value = mock_conn
    push_file("test.txt", b"data", ubus_module=mock_ubus_ok)

    # 2. When UBUS fails, exit(1) is called
    mock_ubus_err = MagicMock()
    mock_ubus_err.connect.return_value = None
    with pytest.raises(SystemExit) as exc_info:
        push_file("test.txt", b"data", ubus_module=mock_ubus_err)
    assert exc_info.value.code == 1


def test_main_cli_validation(tmp_path: Path) -> None:
    test_file = tmp_path / "sample.txt"
    test_file.write_bytes(b"content to push")

    pushed_args: list[tuple[str, bytes]] = []

    def mock_push(target: str, data: bytes) -> None:
        pushed_args.append((target, data))

    orig_handler = _file_push.push_file_handler
    _file_push.push_file_handler = mock_push
    try:
        # Push to Linux path
        cli_main(test_file, "/tmp/sample.txt", mcu=False)
        assert len(pushed_args) == 1
        assert pushed_args[0][0] == "tmp/sample.txt"
        assert pushed_args[0][1] == b"content to push"

        # Push to MCU path
        cli_main(test_file, "/sketch.bin", mcu=True)
        assert len(pushed_args) == 2
        assert pushed_args[1][0] == "mcu/sketch.bin"
    finally:
        _file_push.push_file_handler = orig_handler
