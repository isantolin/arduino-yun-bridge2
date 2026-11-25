"""Unit tests for CI and standalone QEMU modes in tools/emulation/openwrt_smoke_test.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import typer

from tools.emulation import openwrt_smoke_test


def test_build_qemu_cmd_ci_and_standalone_port_forwarding() -> None:
    """CI mode keeps default SLIRP networking; standalone mode adds hostfwd and optional APK disk."""
    ci_cmd = openwrt_smoke_test.build_qemu_cmd("apks.img", "extroot.img")
    assert "file=apks.img,format=raw,if=ide" in ci_cmd
    assert "user,id=net0,ipv6=off" in ci_cmd
    assert "256" in ci_cmd

    standalone_cmd = openwrt_smoke_test.build_qemu_cmd(
        None,
        "extroot.img",
        memory_mb=512,
        ssh_port=2222,
        luci_port=8080,
        gateway_port=5555,
    )
    assert "file=apks.img,format=raw,if=ide" not in standalone_cmd
    assert "512" in standalone_cmd
    netdev_arg = standalone_cmd[standalone_cmd.index("-netdev") + 1]
    assert "hostfwd=tcp::2222-:22" in netdev_arg
    assert "hostfwd=tcp::8080-:80" in netdev_arg
    assert "hostfwd=tcp::5555-:5555" in netdev_arg


def test_main_validation_and_standalone_skip_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI rejects invalid combinations and wires standalone defaults cleanly."""
    with pytest.raises(typer.BadParameter, match="--skip-install is only supported"):
        openwrt_smoke_test.main(mode=openwrt_smoke_test.SmokeMode.CI, skip_install=True)

    with pytest.raises(typer.BadParameter, match="apk_directory is required"):
        openwrt_smoke_test.main(mode=openwrt_smoke_test.SmokeMode.CI, apk_directory=None)

    captured: dict[str, Any] = {}

    def _fake_download(*, reuse_rootfs: bool = False) -> None:
        captured["reuse_rootfs"] = reuse_rootfs

    def _fake_extroot(*, reuse_existing: bool = False) -> str:
        captured["reuse_extroot"] = reuse_existing
        return "extroot.img"

    def _fake_run_test(apk_disk: str | None, extroot_disk: str, **kwargs: Any) -> None:
        captured["apk_disk"] = apk_disk
        captured["extroot_disk"] = extroot_disk
        captured.update(kwargs)

    monkeypatch.setattr(openwrt_smoke_test, "download_images", _fake_download)
    monkeypatch.setattr(openwrt_smoke_test, "create_extroot_disk", _fake_extroot)
    monkeypatch.setattr(openwrt_smoke_test, "run_test", _fake_run_test)

    workdir = tmp_path / "openwrt-vm"
    openwrt_smoke_test.main(
        apk_directory=None,
        mode=openwrt_smoke_test.SmokeMode.STANDALONE,
        workdir=workdir,
        skip_install=True,
        memory_mb=512,
    )

    assert workdir.is_dir()
    assert captured["reuse_rootfs"] is True
    assert captured["reuse_extroot"] is True
    assert captured["apk_disk"] is None
    assert captured["mode"] is openwrt_smoke_test.SmokeMode.STANDALONE
    assert captured["skip_install"] is True
    assert captured["memory_mb"] == 512
    assert captured["ssh_port"] == openwrt_smoke_test.DEFAULT_STANDALONE_SSH_PORT
    assert captured["luci_port"] == openwrt_smoke_test.DEFAULT_STANDALONE_LUCI_PORT
    assert captured["gateway_port"] is None
