"""Unit and pw_emu integration tests for Renode and QEMU migration (Item 2)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from pw_emu.core import Config as PwEmuConfig
from pw_emu.core import Error as PwEmuError
from pw_emu.core import Launcher, NotRunning
from pw_emu.frontend import Emulator
from pw_emu.qemu import QemuLauncher
from pw_emu.renode import RenodeLauncher

from tools.emulation import qemu_runner, renode_runner, test_fleet_mesh_hardware_e2e


def test_pw_emu_config_and_pigweed_json_register_renode_and_qemu_targets() -> None:
    cfg_path = renode_runner.pw_emu_config_path()
    root_cfg_path = renode_runner.repo_root / "pigweed.json"
    assert cfg_path == qemu_runner.pw_emu_config_path()
    assert cfg_path.is_file()
    assert root_cfg_path.is_file()

    for config_file in (cfg_path, root_cfg_path):
        renode_cfg = PwEmuConfig(config_file, target="mkrwifi1010", emu="renode")
        qemu_cfg = PwEmuConfig(config_file, target="esp32s3", emu="qemu")
        renode_cfg.set_target("mkrwifi1010")
        qemu_cfg.set_target("esp32s3")

        targets = renode_cfg.get_targets()
        assert "mkrwifi1010" in targets
        assert "esp32s3" in targets
        assert "atmega2560" in targets

        assert isinstance(Launcher.get("renode", config_file), RenodeLauncher)
        assert isinstance(Launcher.get("qemu", config_file), QemuLauncher)

        pw_emu_data = json.loads(config_file.read_text(encoding="utf-8"))["pw"]["pw_emu"]
        assert pw_emu_data["renode"]["executable"] == "renode"
        mkr_renode = pw_emu_data["targets"]["mkrwifi1010"]["renode"]
        machine_rel = mkr_renode["machine"]
        assert machine_rel == "tools/emulation/renode/mkrwifi1010.repl"
        assert (renode_runner.repo_root / machine_rel).is_file()
        assert mkr_renode["channels"]["terminals"]["uart0"] == {
            "device-path": "sysbus.uart0",
            "type": "pty",
        }

        assert pw_emu_data["qemu"]["executable"] == "qemu-system-xtensa"
        esp_qemu = pw_emu_data["targets"]["esp32s3"]["qemu"]
        assert esp_qemu["machine"] == "esp32s3"
        assert esp_qemu["channels"]["chardevs"]["uart0"] == {
            "id": "serial0",
            "type": "pty",
        }

    emulation_raw = json.loads(cfg_path.read_text(encoding="utf-8"))["pw"]["pw_emu"]
    root_raw = json.loads(root_cfg_path.read_text(encoding="utf-8"))["pw"]["pw_emu"]
    assert emulation_raw["targets"] == root_raw["targets"]
    assert emulation_raw["renode"] == root_raw["renode"]
    assert emulation_raw["qemu"] == root_raw["qemu"]


def test_legacy_fixed_ports_and_resc_are_removed() -> None:
    assert not hasattr(renode_runner, "DEFAULT_RENODE_PORT")
    assert not hasattr(qemu_runner, "DEFAULT_QEMU_PORT")
    assert not hasattr(test_fleet_mesh_hardware_e2e, "PORT_RENODE")
    assert not hasattr(test_fleet_mesh_hardware_e2e, "PORT_QEMU")
    assert not (renode_runner.repo_root / "tools" / "emulation" / "renode" / "mkrwifi1010.resc").exists()


def test_start_renode_emulator_configures_samd21_and_wires_pty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, Any] = {"ops": []}
    fake_renode_dir = tmp_path / "custom_renode_bin"
    fake_renode_dir.mkdir()
    fake_renode = fake_renode_dir / "renode"
    fake_renode.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_renode.chmod(0o755)
    original_path = os.environ.get("PATH", "")

    class _FakeRenodeEmulator:
        def __init__(self, wdir: Path, config_path: Path | None = None) -> None:
            recorded["wdir"] = wdir
            recorded["config_path"] = config_path

        def start(
            self,
            target: str,
            file: Path | None = None,
            pause: bool = False,
            args: str | None = None,
        ) -> None:
            recorded["start"] = (target, file, pause, args)
            recorded["path_during_start"] = os.environ.get("PATH", "").split(os.pathsep)[0]

        def get_channel_path(self, name: str) -> str:
            recorded["channel"] = name
            return str(recorded["wdir"] / name)

        def get_property(self, path: str, prop: str) -> Any:
            recorded["ops"].append(("get_property", path, prop))
            if path == "sysbus" and prop == "ReadDoubleWord 0x2000":
                return "0x20008000\n\n"
            if path == "sysbus" and prop == "ReadDoubleWord 0x2004":
                return "0x00002009\n\n"
            return ""

        def set_property(self, path: str, prop: str, value: Any) -> None:
            recorded["ops"].append(("set_property", path, prop, value))

        def cont(self) -> None:
            recorded["ops"].append(("cont",))

        def stop(self) -> None:
            recorded["stopped"] = True
            raise NotRunning(recorded["wdir"])

    monkeypatch.setattr(renode_runner, "find_renode_binary", lambda: str(fake_renode))
    monkeypatch.setattr(renode_runner, "Emulator", _FakeRenodeEmulator)

    fw = tmp_path / "firmware.elf"
    fw.write_bytes(b"\x7fELF")
    wdir = tmp_path / "renode_wdir"

    emu, pty_path = renode_runner.start_renode_emulator(wdir, fw)
    assert pty_path == str(wdir / "uart0")
    assert recorded["wdir"] == wdir
    assert recorded["config_path"] == renode_runner.pw_emu_config_path()
    assert recorded["start"] == ("mkrwifi1010", None, True, None)
    assert recorded["channel"] == "uart0"
    assert recorded["path_during_start"] == str(fake_renode_dir)
    assert os.environ.get("PATH", "") == original_path

    assert recorded["ops"] == [
        ("get_property", "emulation", "PauseAll"),
        ("get_property", "machine", "Reset"),
        ("set_property", "sysbus", "LoadELF", f"@{fw.resolve()}"),
        ("set_property", "sysbus.cpu", "VectorTableOffset", "0x2000"),
        ("get_property", "sysbus", "ReadDoubleWord 0x2000"),
        ("get_property", "sysbus", "ReadDoubleWord 0x2004"),
        ("set_property", "sysbus.cpu", "SP", "0x20008000"),
        ("set_property", "sysbus.cpu", "PC", "0x00002009"),
        ("set_property", "sysbus", "WriteDoubleWord", "0x4000080c 0x0000ffff"),
        ("set_property", "sysbus", "WriteDoubleWord", "0x00806020 0xffffffff"),
        ("set_property", "sysbus", "WriteDoubleWord", "0x00806024 0xffffffff"),
        ("set_property", "sysbus", "WriteDoubleWord", "0x41004014 0x00000001"),
        ("cont",),
    ]

    # pause=True leaves Renode paused after SAMD21 init.
    recorded["ops"] = []
    renode_runner.start_renode_emulator(tmp_path / "renode_paused", fw, pause=True)
    assert ("cont",) not in recorded["ops"]

    # stop_renode_emulator tolerates NotRunning and None cleanly.
    renode_runner.stop_renode_emulator(emu)
    assert recorded["stopped"] is True
    renode_runner.stop_renode_emulator(None)


def test_start_renode_emulator_cleans_up_on_setup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped: list[Path] = []

    class _FailingRenodeEmulator:
        def __init__(self, wdir: Path, config_path: Path | None = None) -> None:
            self.wdir = wdir
            _ = config_path

        def start(
            self,
            target: str,
            file: Path | None = None,
            pause: bool = False,
            args: str | None = None,
        ) -> None:
            _ = (target, file, pause, args)

        def get_channel_path(self, name: str) -> str:
            return str(self.wdir / name)

        def get_property(self, path: str, prop: str) -> Any:
            _ = (path, prop)
            return "   \n"

        def set_property(self, path: str, prop: str, value: Any) -> None:
            _ = (path, prop, value)

        def stop(self) -> None:
            stopped.append(self.wdir)

    monkeypatch.setattr(renode_runner, "find_renode_binary", lambda: None)
    fw = tmp_path / "firmware.elf"
    fw.write_bytes(b"\x7fELF")
    with pytest.raises(PwEmuError, match="Renode executable not found"):
        renode_runner.start_renode_emulator(tmp_path / "no_bin", fw)

    monkeypatch.setattr(renode_runner, "find_renode_binary", lambda: "/usr/local/bin/renode")
    monkeypatch.setattr(renode_runner, "Emulator", _FailingRenodeEmulator)
    wdir = tmp_path / "failing_wdir"
    with pytest.raises(PwEmuError, match="returned empty SP/PC"):
        renode_runner.start_renode_emulator(wdir, fw)
    assert stopped == [wdir]


def test_start_renode_emulator_resolves_machine_path_outside_repo_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_cfg: list[Path] = []

    class _FakeEmulator:
        def __init__(self, wdir: Path, config_path: Path | None = None) -> None:
            self.wdir = wdir
            assert config_path is not None
            captured_cfg.append(config_path)

        def start(
            self,
            target: str,
            file: Path | None = None,
            pause: bool = False,
            args: str | None = None,
        ) -> None:
            _ = (target, file, pause, args)

        def get_channel_path(self, name: str) -> str:
            return str(self.wdir / name)

        def get_property(self, path: str, prop: str) -> Any:
            _ = (path, prop)
            return "0x20008000\n"

        def set_property(self, path: str, prop: str, value: Any) -> None:
            _ = (path, prop, value)

        def cont(self) -> None:
            return None

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(renode_runner, "find_renode_binary", lambda: "/usr/local/bin/renode")
    monkeypatch.setattr(renode_runner, "Emulator", _FakeEmulator)

    fw = tmp_path / "firmware.elf"
    fw.write_bytes(b"\x7fELF")
    wdir = tmp_path / "outside_wdir"
    renode_runner.start_renode_emulator(wdir, fw)

    assert len(captured_cfg) == 1
    resolved_cfg = PwEmuConfig(captured_cfg[0], target="mkrwifi1010", emu="renode")
    resolved_cfg.set_target("mkrwifi1010")
    resolved_raw = json.loads(captured_cfg[0].read_text(encoding="utf-8"))
    machine_path = Path(resolved_raw["pw"]["pw_emu"]["targets"]["mkrwifi1010"]["renode"]["machine"])
    assert machine_path.is_absolute()
    assert machine_path.is_file()
    assert machine_path.name == "mkrwifi1010.repl"
    assert "atmega2560" in resolved_cfg.get_targets()


def test_build_esp32s3_drive_arg_handles_standard_and_spaced_paths(tmp_path: Path) -> None:
    plain_img = tmp_path / "firmware_flash.bin"
    plain_img.write_bytes(b"\xff" * 16)
    assert qemu_runner.build_esp32s3_drive_arg(plain_img) == f"-drive file={plain_img.resolve()},if=mtd,format=raw"

    spaced_dir = tmp_path / "dir with spaces"
    spaced_dir.mkdir()
    spaced_img = spaced_dir / "firmware flash.bin"
    spaced_img.write_bytes(b"\xff" * 16)
    wdir = tmp_path / "wdir_no_spaces"
    arg = qemu_runner.build_esp32s3_drive_arg(spaced_img, wdir=wdir)
    expected_link = wdir / "firmware_flash.bin"
    assert arg == f"-drive file={expected_link},if=mtd,format=raw"
    assert expected_link.is_symlink()
    assert expected_link.resolve() == spaced_img.resolve()


def test_start_qemu_emulator_uses_mtd_drive_pty_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, Any] = {"cont_calls": 0}
    fake_qemu_dir = tmp_path / "esp_qemu_bin"
    fake_qemu_dir.mkdir()
    fake_qemu = fake_qemu_dir / "qemu-system-xtensa"
    fake_qemu.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_qemu.chmod(0o755)
    original_path = os.environ.get("PATH", "")

    class _FakeQemuEmulator:
        def __init__(self, wdir: Path, config_path: Path | None = None) -> None:
            recorded["wdir"] = wdir
            recorded["config_path"] = config_path

        def start(
            self,
            target: str,
            file: Path | None = None,
            pause: bool = False,
            args: str | None = None,
        ) -> None:
            recorded["start"] = (target, file, pause, args)
            recorded["path_during_start"] = os.environ.get("PATH", "").split(os.pathsep)[0]

        def get_channel_path(self, name: str) -> str:
            recorded["channel"] = name
            return "/dev/pts/88"

        def cont(self) -> None:
            recorded["cont_calls"] += 1

        def stop(self) -> None:
            recorded["stopped"] = True
            raise NotRunning(recorded["wdir"])

    monkeypatch.setattr(qemu_runner, "find_qemu_binary", lambda: str(fake_qemu))
    monkeypatch.setattr(qemu_runner, "Emulator", _FakeQemuEmulator)

    flash_img = tmp_path / "firmware_flash.bin"
    flash_img.write_bytes(b"\xff" * 64)
    wdir = tmp_path / "qemu_wdir"

    emu, pty_path = qemu_runner.start_qemu_emulator(wdir, flash_img)
    assert pty_path == "/dev/pts/88"
    assert recorded["wdir"] == wdir
    assert recorded["config_path"] == qemu_runner.pw_emu_config_path()
    assert recorded["start"] == (
        "esp32s3",
        None,
        True,
        f"-drive file={flash_img.resolve()},if=mtd,format=raw",
    )
    assert recorded["channel"] == "uart0"
    assert recorded["cont_calls"] == 1
    assert recorded["path_during_start"] == str(fake_qemu_dir)
    assert os.environ.get("PATH", "") == original_path

    # pause=True leaves QEMU paused at -S without issuing QMP cont.
    qemu_runner.start_qemu_emulator(tmp_path / "qemu_paused", flash_img, pause=True)
    assert recorded["cont_calls"] == 1

    # stop_qemu_emulator tolerates NotRunning and None cleanly.
    qemu_runner.stop_qemu_emulator(emu)
    assert recorded["stopped"] is True
    qemu_runner.stop_qemu_emulator(None)


def test_start_qemu_emulator_cleans_up_on_error_and_missing_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped: list[Path] = []

    class _FailingQemuEmulator:
        def __init__(self, wdir: Path, config_path: Path | None = None) -> None:
            self.wdir = wdir
            _ = config_path

        def start(
            self,
            target: str,
            file: Path | None = None,
            pause: bool = False,
            args: str | None = None,
        ) -> None:
            _ = (target, file, pause, args)

        def get_channel_path(self, name: str) -> str:
            raise PwEmuError(f"channel {name} unavailable")

        def stop(self) -> None:
            stopped.append(self.wdir)

    flash_img = tmp_path / "firmware_flash.bin"
    flash_img.write_bytes(b"\xff" * 64)

    monkeypatch.setattr(qemu_runner, "find_qemu_binary", lambda: None)
    with pytest.raises(PwEmuError, match="qemu-system-xtensa"):
        qemu_runner.start_qemu_emulator(tmp_path / "missing_bin", flash_img)

    monkeypatch.setattr(qemu_runner, "find_qemu_binary", lambda: "/opt/qemu-esp32/bin/qemu-system-xtensa")
    monkeypatch.setattr(qemu_runner, "Emulator", _FailingQemuEmulator)
    wdir = tmp_path / "failing_qemu"
    with pytest.raises(PwEmuError, match="channel uart0 unavailable"):
        qemu_runner.start_qemu_emulator(wdir, flash_img)
    assert stopped == [wdir]


def test_fleet_mesh_verify_host_binaries_and_watchdog_without_socat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_simavr = tmp_path / "simavr_pty_runner"
    fake_simavr.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(test_fleet_mesh_hardware_e2e, "build_simavr_runner", lambda: fake_simavr)
    monkeypatch.setattr(test_fleet_mesh_hardware_e2e, "find_renode_binary", lambda: "/usr/local/bin/renode")
    monkeypatch.setattr(
        test_fleet_mesh_hardware_e2e,
        "find_qemu_binary",
        lambda: "/opt/qemu-esp32/bin/qemu-system-xtensa",
    )

    assert test_fleet_mesh_hardware_e2e.verify_host_binaries() == (
        fake_simavr,
        "/usr/local/bin/renode",
        "/opt/qemu-esp32/bin/qemu-system-xtensa",
    )

    monkeypatch.setattr(test_fleet_mesh_hardware_e2e, "find_renode_binary", lambda: None)
    assert test_fleet_mesh_hardware_e2e.verify_host_binaries() is None

    monkeypatch.setattr(test_fleet_mesh_hardware_e2e, "find_renode_binary", lambda: "/usr/local/bin/renode")
    monkeypatch.setattr(test_fleet_mesh_hardware_e2e, "find_qemu_binary", lambda: None)
    assert test_fleet_mesh_hardware_e2e.verify_host_binaries() is None

    cfg_path = renode_runner.pw_emu_config_path()
    for name in ("simavr", "renode", "qemu"):
        emu = Emulator(tmp_path / f"dead_{name}", config_path=cfg_path)
        assert test_fleet_mesh_hardware_e2e.emulator_exit_reason(name, emu) == (
            f"emulator {name} exited during verification"
        )
