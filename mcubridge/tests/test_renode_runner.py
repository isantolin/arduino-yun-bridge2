"""Unit tests for tools/emulation/renode_runner.py (SIL-2 / Rule 11 / Rule 18 / Rule 37)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import typer

from tools.emulation import renode_runner
from tools.arduino_core_metadata import ArduinoCoreMetadata, write_metadata_json


def _write_board_metadata(output_dir: Path, fqbn: str) -> None:
    write_metadata_json(
        ArduinoCoreMetadata(
            fqbn=fqbn,
            mcu="cortex-m0plus",
            frequency_hz=48_000_000,
            led_builtin=6,
            digital_pins=14,
            analog_inputs=7,
            spi_ss=4,
            spi_mosi=8,
            spi_miso=10,
            spi_sck=9,
            i2c_sda=11,
            i2c_scl=12,
        ),
        output_dir / "arduino_core_metadata.json",
    )


def test_renode_state_on_line() -> None:
    state = renode_runner.RenodeState()
    assert not state.sync_event.is_set()

    state.on_line("Handshake synchronization complete", "daemon-stdout")
    assert state.sync_event.is_set()

    state.on_line("Capabilities discovery complete", "daemon-stdout")
    assert state.capabilities_event.is_set()


def test_run_single_client_script_with_main(tmp_path: Path) -> None:
    script = tmp_path / "test_client.py"
    result_file = tmp_path / "led_pin.txt"
    script.write_text(
        "from pathlib import Path\n"
        "def main(host=None, port=None, device_id=None, led_builtin_pin=None):\n"
        f"    Path({str(result_file)!r}).write_text(str(device_id) + ':' + str(led_builtin_pin), encoding='utf-8')\n",
        encoding="utf-8",
    )
    passed = renode_runner.run_single_client_script(
        script,
        device_id="mkr-01",
        led_builtin_pin=6,
    )
    assert passed is True
    assert result_file.read_text(encoding="utf-8") == "mkr-01:6"


def test_run_single_client_script_with_exit_code(tmp_path: Path) -> None:
    script_pass = tmp_path / "pass_exit.py"
    script_pass.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    assert renode_runner.run_single_client_script(script_pass) is True

    script_fail = tmp_path / "fail_exit.py"
    script_fail.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    assert renode_runner.run_single_client_script(script_fail) is False


def test_run_single_client_script_exception(tmp_path: Path) -> None:
    script_err = tmp_path / "error_script.py"
    script_err.write_text(
        "def main(host=None, port=None, device_id=None):\n    raise RuntimeError('boom')\n",
        encoding="utf-8",
    )
    assert renode_runner.run_single_client_script(script_err) is False


def test_run_client_scripts(tmp_path: Path) -> None:
    s1 = tmp_path / "s1.py"
    s1.write_text("def main(host=None, port=None, device_id=None):\n    pass\n", encoding="utf-8")
    missing = tmp_path / "does_not_exist.py"

    passed = renode_runner.run_client_scripts([s1, missing], {}, 10.0, led_builtin_pin=6)
    assert passed is True


def _mock_renode_success(
    firmware_path: Path,
    test_scripts: list[Path],
    timeout_seconds: float = 90.0,
    port: int = 12345,
    led_builtin_pin: int = 6,
) -> bool:
    _ = (firmware_path, test_scripts, timeout_seconds, port, led_builtin_pin)
    return True


def _mock_renode_failure(
    firmware_path: Path,
    test_scripts: list[Path],
    timeout_seconds: float = 90.0,
    port: int = 12345,
    led_builtin_pin: int = 6,
) -> bool:
    _ = (firmware_path, test_scripts, timeout_seconds, port, led_builtin_pin)
    return False


def _mock_which_renode_and_socat(cmd: str) -> str | None:
    return "/usr/bin/renode" if cmd == "renode" else "/usr/bin/socat"


def _mock_which_socat_only(cmd: str) -> str | None:
    return "/usr/bin/socat" if cmd == "socat" else None


def _mock_which_renode_only(cmd: str) -> str | None:
    return "/usr/bin/renode" if cmd == "renode" else None


def test_run_matrix_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renode_runner, "repo_root", tmp_path)
    sketch = tmp_path / "TestSketch.ino"
    sketch.write_text("// test sketch\n", encoding="utf-8")

    def mock_compile_run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        _ = check
        out_dir = Path(cmd[4])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "firmware.elf").write_text("ELF_BINARY", encoding="utf-8")
        _write_board_metadata(out_dir, cmd[3])
        return subprocess.CompletedProcess(cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_compile_run)
    monkeypatch.setattr(renode_runner, "run_renode_emulation", _mock_renode_success)
    monkeypatch.setattr(renode_runner.shutil, "which", _mock_which_renode_and_socat)
    monkeypatch.setenv("RENODE_METRICS_DIR", str(tmp_path / "metrics"))

    fail_count = renode_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 0
    assert renode_runner.MATRIX_BOARDS == [("arduino:samd:mkrwifi1010", "Arduino MKR WiFi 1010 (ATSAMD21G18A)")]

    summary_file = tmp_path / "metrics" / "renode_summary.md"
    assert summary_file.exists()
    content = summary_file.read_text(encoding="utf-8")
    assert "Passed (100% E2E)" in content
    assert "Arduino MKR WiFi 1010" in content
    assert "PASS" in content


def test_run_matrix_renode_not_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renode_runner, "repo_root", tmp_path)
    sketch = tmp_path / "TestSketch.ino"
    sketch.write_text("// test sketch\n", encoding="utf-8")

    def mock_compile_run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        _ = check
        out_dir = Path(cmd[4])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "firmware.elf").write_text("ELF_BINARY", encoding="utf-8")
        _write_board_metadata(out_dir, cmd[3])
        return subprocess.CompletedProcess(cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_compile_run)
    monkeypatch.setattr(renode_runner.shutil, "which", _mock_which_socat_only)
    monkeypatch.setenv("RENODE_METRICS_DIR", str(tmp_path / "metrics"))

    fail_count = renode_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 0

    summary_file = tmp_path / "metrics" / "renode_summary.md"
    content = summary_file.read_text(encoding="utf-8")
    assert "Skipped (Renode binary not installed)" in content
    assert "SKIPPED" in content


def test_run_matrix_compilation_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renode_runner, "repo_root", tmp_path)
    sketch = tmp_path / "TestSketch.ino"
    sketch.write_text("// test sketch\n", encoding="utf-8")

    def mock_compile_error(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        _ = check
        return subprocess.CompletedProcess(cmd, returncode=1)

    monkeypatch.setattr(subprocess, "run", mock_compile_error)
    monkeypatch.setenv("RENODE_METRICS_DIR", str(tmp_path / "metrics"))

    fail_count = renode_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 1

    summary_file = tmp_path / "metrics" / "renode_summary.md"
    content = summary_file.read_text(encoding="utf-8")
    assert "FAIL" in content
    assert "compilation failed" in content


def test_run_matrix_emulation_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renode_runner, "repo_root", tmp_path)
    sketch = tmp_path / "TestSketch.ino"
    sketch.write_text("// test sketch\n", encoding="utf-8")

    def mock_compile_run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        _ = check
        out_dir = Path(cmd[4])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "firmware.elf").write_text("ELF_BINARY", encoding="utf-8")
        _write_board_metadata(out_dir, cmd[3])
        return subprocess.CompletedProcess(cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_compile_run)
    monkeypatch.setattr(renode_runner, "run_renode_emulation", _mock_renode_failure)
    monkeypatch.setattr(renode_runner.shutil, "which", _mock_which_renode_only)
    monkeypatch.setenv("RENODE_METRICS_DIR", str(tmp_path / "metrics"))

    fail_count = renode_runner.run_matrix(sketch, timeout_seconds=5.0, test_scripts=[])
    assert fail_count == 1

    summary_file = tmp_path / "metrics" / "renode_summary.md"
    content = summary_file.read_text(encoding="utf-8")
    assert "FAIL" in content


def test_run_renode_emulation_missing_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_elf = tmp_path / "firmware.elf"
    fake_elf.write_text("ELF", encoding="utf-8")

    # Missing renode
    monkeypatch.setattr(renode_runner.shutil, "which", _mock_which_socat_only)
    assert renode_runner.run_renode_emulation(fake_elf, []) is False

    # Missing socat
    monkeypatch.setattr(renode_runner.shutil, "which", _mock_which_renode_only)
    assert renode_runner.run_renode_emulation(fake_elf, []) is False


def test_main_rejects_missing_sketch(tmp_path: Path) -> None:
    missing_sketch = tmp_path / "missing.ino"
    with pytest.raises(typer.BadParameter, match="Sketch does not exist"):
        renode_runner.main(sketch=missing_sketch)


def test_renode_state_on_line_capabilities_received() -> None:
    state = renode_runner.RenodeState()
    assert not state.capabilities_event.is_set()
    state.on_line("MCU capabilities received: ...", "daemon-stdout")
    assert state.capabilities_event.is_set()
    assert state.sync_event.is_set()
    # Empty line ignored
    state.on_line("   ", "daemon-stdout")


def test_ensure_cloud_gateway_already_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    state = renode_runner.RenodeState()

    def mock_tcp_ready(host: str, port: int, timeout: float = 30.0, interval: float = 0.5) -> bool:
        _ = (host, port, timeout, interval)
        return True

    monkeypatch.setattr(renode_runner, "wait_for_tcp_ready", mock_tcp_ready)
    proc = renode_runner.ensure_cloud_gateway(state)
    assert proc is None


def test_ensure_cloud_gateway_spawn_success(monkeypatch: pytest.MonkeyPatch) -> None:
    state = renode_runner.RenodeState()
    ready_sequence = [False, True]

    def mock_tcp_ready(host: str, port: int, timeout: float = 30.0, interval: float = 0.5) -> bool:
        _ = (host, port, timeout, interval)
        return ready_sequence.pop(0)

    monkeypatch.setattr(renode_runner, "wait_for_tcp_ready", mock_tcp_ready)

    class MockPopen:
        stdout = None

    def mock_popen(*args: Any, **kwargs: Any) -> MockPopen:
        _ = (args, kwargs)
        return MockPopen()

    def mock_start_thread(*args: Any, **kwargs: Any) -> None:
        _ = (args, kwargs)

    monkeypatch.setattr(subprocess, "Popen", mock_popen)
    monkeypatch.setattr(renode_runner, "start_daemon_thread", mock_start_thread)

    proc = renode_runner.ensure_cloud_gateway(state)
    assert proc is not None


def test_ensure_cloud_gateway_spawn_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    state = renode_runner.RenodeState()

    def mock_tcp_ready(host: str, port: int, timeout: float = 30.0, interval: float = 0.5) -> bool:
        _ = (host, port, timeout, interval)
        return False

    monkeypatch.setattr(renode_runner, "wait_for_tcp_ready", mock_tcp_ready)

    class MockPopen:
        stdout = None

    def mock_popen(*args: Any, **kwargs: Any) -> MockPopen:
        _ = (args, kwargs)
        return MockPopen()

    terminated: list[bool] = []

    def mock_terminate(procs: Any, timeout: float = 1.0) -> None:
        _ = (procs, timeout)
        terminated.append(True)

    def mock_start_thread(*args: Any, **kwargs: Any) -> None:
        _ = (args, kwargs)

    monkeypatch.setattr(subprocess, "Popen", mock_popen)
    monkeypatch.setattr(renode_runner, "start_daemon_thread", mock_start_thread)
    monkeypatch.setattr(renode_runner, "terminate_process_tree", mock_terminate)

    proc = renode_runner.ensure_cloud_gateway(state)
    assert proc is None
    assert len(terminated) == 1


def test_run_single_client_script_runpy_fallback(tmp_path: Path) -> None:
    script = tmp_path / "test_runpy.py"
    output_file = tmp_path / "out.txt"
    script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"Path({str(output_file)!r}).write_text(' '.join(sys.argv), encoding='utf-8')\n",
        encoding="utf-8",
    )
    passed = renode_runner.run_single_client_script(script, device_id="mkr-special")
    assert passed is True
    assert "--device-id mkr-special" in output_file.read_text(encoding="utf-8")
