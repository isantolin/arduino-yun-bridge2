"""Unit tests for asyncssh, openwrt-luci-rpc, and pytest-embedded integrations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_embedded.unity import TestSuite as UnityTestSuite

from tools.audit import validate_luci
from tools.emulation import hardware_harness, sync_to_vm

UnityTestSuite.__test__ = False


def test_validate_luci_rpc_endpoint_success() -> None:
    mock_rpc = MagicMock()
    mock_rpc.is_logged_in.return_value = True

    with patch("openwrt_luci_rpc.OpenWrtRpc", return_value=mock_rpc):
        code = validate_luci.validate_luci_rpc_endpoint("192.168.1.1", "root", "secret")
        assert code == 0
        mock_rpc.is_logged_in.assert_called_once()


def test_validate_luci_rpc_endpoint_login_failure() -> None:
    mock_rpc = MagicMock()
    mock_rpc.is_logged_in.return_value = False

    with patch("openwrt_luci_rpc.OpenWrtRpc", return_value=mock_rpc):
        code = validate_luci.validate_luci_rpc_endpoint("192.168.1.1", "root", "wrong")
        assert code == 1
        mock_rpc.is_logged_in.assert_called_once()


def test_validate_luci_rpc_endpoint_exception_handling() -> None:
    from openwrt_luci_rpc.exceptions import InvalidLuciLoginError

    with patch("openwrt_luci_rpc.OpenWrtRpc", side_effect=InvalidLuciLoginError("Bad auth")):
        code = validate_luci.validate_luci_rpc_endpoint("192.168.1.1", "root", "bad")
        assert code == 1


def test_run_remote_ssh_success() -> None:
    mock_conn = AsyncMock()
    mock_res = MagicMock()
    mock_res.exit_status = 0
    mock_res.stdout = "output-ok"
    mock_res.stderr = ""
    mock_conn.run.return_value = mock_res

    mock_connect = AsyncMock()
    mock_connect.__aenter__.return_value = mock_conn

    with patch("asyncssh.connect", return_value=mock_connect):
        exit_code, stdout, stderr = sync_to_vm.run_remote_ssh("echo test", "192.168.122.200", user="root")
        assert exit_code == 0
        assert stdout == "output-ok"
        assert stderr == ""
        mock_conn.run.assert_awaited_once_with("echo test")


def test_run_remote_ssh_error_with_check() -> None:
    mock_conn = AsyncMock()
    mock_res = MagicMock()
    mock_res.exit_status = 127
    mock_res.stdout = ""
    mock_res.stderr = "command not found"
    mock_conn.run.return_value = mock_res

    mock_connect = AsyncMock()
    mock_connect.__aenter__.return_value = mock_conn

    with (
        patch("asyncssh.connect", return_value=mock_connect),
        pytest.raises(RuntimeError, match="Remote command failed on 192.168.122.200"),
    ):
        sync_to_vm.run_remote_ssh("bad_cmd", "192.168.122.200", user="root", check=True)


def test_sync_push_file_sftp(tmp_path: Path) -> None:
    test_file = tmp_path / "hello.txt"
    test_file.write_text("content", encoding="utf-8")

    mock_sftp = AsyncMock()
    mock_conn = AsyncMock()
    mock_sftp_ctx = MagicMock()
    mock_sftp_ctx.__aenter__ = AsyncMock(return_value=mock_sftp)
    mock_sftp_ctx.__aexit__ = AsyncMock(return_value=None)
    mock_conn.start_sftp_client = MagicMock(return_value=mock_sftp_ctx)

    mock_connect = AsyncMock()
    mock_connect.__aenter__.return_value = mock_conn

    with patch("asyncssh.connect", return_value=mock_connect):
        sync_to_vm.push_file(test_file, "/tmp/hello.txt", "192.168.122.200", "root", mode="0755")
        mock_sftp.makedirs.assert_awaited_once_with("/tmp", exist_ok=True)
        mock_sftp.put.assert_awaited_once_with(str(test_file), "/tmp/hello.txt")
        mock_sftp.chmod.assert_awaited_once_with("/tmp/hello.txt", 0o755)


def test_tar_push_asyncssh(tmp_path: Path) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "sample.py").write_text("print('sample')\n", encoding="utf-8")

    mock_proc = AsyncMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdin.write = MagicMock()
    mock_proc.stdin.drain = AsyncMock()
    mock_proc.stdin.write_eof = MagicMock()
    mock_proc.wait = AsyncMock()
    mock_proc.exit_status = 0

    mock_conn = AsyncMock()
    mock_conn.run = AsyncMock()
    mock_conn.create_process.return_value = mock_proc

    mock_connect = AsyncMock()
    mock_connect.__aenter__.return_value = mock_conn

    with patch("asyncssh.connect", return_value=mock_connect):
        sync_to_vm.tar_push(source_dir, "/tmp/target", "192.168.122.200", "root")
        mock_conn.run.assert_awaited_once_with("mkdir -p '/tmp/target'", check=True)
        mock_conn.create_process.assert_awaited_once_with("tar -xzf - -C '/tmp/target'")
        assert mock_proc.stdin.write.called
        mock_proc.stdin.drain.assert_awaited_once()
        mock_proc.stdin.write_eof.assert_called_once()
        mock_proc.wait.assert_awaited_once()


def test_hardware_harness_run_ssh_command() -> None:
    mock_conn = AsyncMock()
    mock_res = MagicMock()
    mock_res.exit_status = 0
    mock_res.stdout = "harness-output"
    mock_res.stderr = ""
    mock_conn.run.return_value = mock_res

    mock_connect = AsyncMock()
    mock_connect.__aenter__.return_value = mock_conn

    with patch("asyncssh.connect", return_value=mock_connect):
        code, out, err = asyncio.run(hardware_harness.run_ssh_command("192.168.122.200", "root", "uptime", timeout=5.0))
        assert code == 0
        assert out == "harness-output"
        assert err == ""
        mock_conn.run.assert_awaited_once_with("uptime")


def test_pytest_embedded_unity_parser_success() -> None:
    unity_raw_output = """
tests/test_core.cpp:10:test_handshake_auth:PASS
tests/test_core.cpp:25:test_mailbox_exchange:PASS
-----------------------
2 Tests 0 Failures 0 Ignored
OK
"""
    suite = UnityTestSuite("unity_suite")
    suite.add_unity_test_cases(unity_raw_output)

    assert len(suite.testcases) == 2
    assert suite.testcases[0].name == "test_handshake_auth"
    assert suite.testcases[0].result == "PASS"
    assert suite.testcases[1].name == "test_mailbox_exchange"
    assert suite.testcases[1].result == "PASS"
    assert len(suite.failed_cases) == 0

    xml = suite.to_xml()
    assert xml.tag == "testsuite"
    assert xml.attrib["tests"] == "2"
    assert xml.attrib["failures"] == "0"


def test_pytest_embedded_unity_parser_failure() -> None:
    unity_raw_output = """
tests/test_core.cpp:12:test_expected_assertion:FAIL:Expected 42 Was 0
-----------------------
1 Tests 1 Failures 0 Ignored
FAIL
"""
    suite = UnityTestSuite("unity_failed_suite")
    suite.add_unity_test_cases(unity_raw_output)

    assert len(suite.testcases) == 1
    assert suite.testcases[0].name == "test_expected_assertion"
    assert suite.testcases[0].result == "FAIL"
    assert len(suite.failed_cases) == 1

    xml = suite.to_xml()
    assert xml.attrib["failures"] == "1"


def test_hardware_harness_rotate_local(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_uci_dir = tmp_path / "etc" / "config"
    fake_uci_dir.mkdir(parents=True)
    mock_mod = MagicMock()
    mock_mod.generate_and_apply_credentials.return_value = ("aabbccddeeff11223344556677889900", "cloudpass12345")
    monkeypatch.setattr(hardware_harness, "_load_rotate_module", lambda: mock_mod)
    hardware_harness.rotate(local=fake_uci_dir, length=32, force=True, no_restart=True)
    captured = capsys.readouterr()
    assert "BRIDGE_SERIAL_SHARED_SECRET" in captured.out
    assert "aabbccddeeff11223344556677889900" in captured.out
