"""Unit tests for mcubridge_file_push script (SIL-2)."""

from __future__ import annotations
import importlib.util
from pathlib import Path
from typing import Any, Callable, cast
from unittest.mock import MagicMock

from pytest_mock import MockerFixture
import pytest

# Dynamically load the standalone script
_script_path = Path(__file__).resolve().parent.parent / "scripts" / "mcubridge_file_push.py"
_spec = importlib.util.spec_from_file_location("mcubridge_file_push", str(_script_path))
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load mcubridge_file_push.py")
_file_push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_file_push)

push_file = cast(Callable[[str, bytes], None], getattr(_file_push, "push_file"))
push_file_ubus = cast(Callable[[str, bytes], bool], getattr(_file_push, "push_file_ubus"))
cli_main = cast(Callable[..., None], getattr(_file_push, "main"))


def test_push_file_ubus_success(mocker: MockerFixture) -> None:
    mock_ubus: Any = MagicMock()
    mock_conn: Any = MagicMock()
    mock_conn.call.return_value = {"status": "ok", "path": "test.txt", "bytes_written": 5}
    mock_ubus.connect.return_value = mock_conn

    def mock_import(name: str) -> Any:
        if name == "ubus":
            return mock_ubus
        return None

    mocker.patch.object(_file_push.importlib, "import_module", side_effect=mock_import)

    res = push_file_ubus("test.txt", b"hello")
    assert res is True
    assert mock_conn.call.called
    call_args = mock_conn.call.call_args[0]
    assert call_args[0] == "mcubridge"
    assert call_args[1] == "file_write"
    assert call_args[2] == {"path": "test.txt", "data": "hello"}


def test_push_file_ubus_binary_hex_fallback(mocker: MockerFixture) -> None:
    mock_ubus: Any = MagicMock()
    mock_conn: Any = MagicMock()
    mock_conn.call.return_value = {"status": "ok", "path": "bin.dat", "bytes_written": 3}
    mock_ubus.connect.return_value = mock_conn

    def mock_import(name: str) -> Any:
        if name == "ubus":
            return mock_ubus
        return None

    mocker.patch.object(_file_push.importlib, "import_module", side_effect=mock_import)

    res = push_file_ubus("bin.dat", b"\xff\xfe\x00")
    assert res is True
    call_args = mock_conn.call.call_args[0]
    assert call_args[2]["data"] == "fffe00"


def test_push_file_ubus_failure_returns_false(mocker: MockerFixture) -> None:
    mock_ubus: Any = MagicMock()
    mock_conn: Any = MagicMock()
    mock_conn.call.return_value = {"status": "error"}
    mock_ubus.connect.return_value = mock_conn

    def mock_import(name: str) -> Any:
        if name == "ubus":
            return mock_ubus
        return None

    mocker.patch.object(_file_push.importlib, "import_module", side_effect=mock_import)
    assert push_file_ubus("test.txt", b"data") is False

    # Connection failure
    mock_ubus.connect.return_value = None
    assert push_file_ubus("test.txt", b"data") is False

    # Exception
    mock_ubus.connect.side_effect = OSError("Connection refused")
    assert push_file_ubus("test.txt", b"data") is False


def test_push_file_direct_ubus(mocker: MockerFixture) -> None:
    # 1. When UBUS succeeds
    mocker.patch.object(_file_push, "push_file_ubus", return_value=True)
    push_file("test.txt", b"data")

    # 2. When UBUS fails, exit(1) is called
    mocker.patch.object(_file_push, "push_file_ubus", return_value=False)
    with pytest.raises(SystemExit) as exc_info:
        push_file("test.txt", b"data")
    assert exc_info.value.code == 1


def test_main_cli_validation(tmp_path: Path, mocker: MockerFixture) -> None:
    test_file = tmp_path / "sample.txt"
    test_file.write_bytes(b"content to push")

    pushed_args: list[tuple[str, bytes]] = []

    def mock_push(target: str, data: bytes) -> None:
        pushed_args.append((target, data))

    mocker.patch.object(_file_push, "push_file", side_effect=mock_push)

    # Push to Linux path
    cli_main(test_file, "/tmp/sample.txt", mcu=False)
    assert len(pushed_args) == 1
    assert pushed_args[0][0] == "tmp/sample.txt"
    assert pushed_args[0][1] == b"content to push"

    # Push to MCU path
    cli_main(test_file, "/sketch.bin", mcu=True)
    assert len(pushed_args) == 2
    assert pushed_args[1][0] == "mcu/sketch.bin"
