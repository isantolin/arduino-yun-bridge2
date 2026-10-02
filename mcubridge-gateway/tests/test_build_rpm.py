"""Unit tests for build_rpm tool (SIL-2 / Rule 4 / Rule 11 / Rule 18)."""

from __future__ import annotations

import importlib.util
import tarfile
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

_build_rpm_path = Path(__file__).resolve().parents[1] / "build_rpm.py"
_spec = importlib.util.spec_from_file_location("build_rpm", _build_rpm_path)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Failed to load build_rpm module")
build_rpm: ModuleType = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_rpm)


def test_generate_spec_file(tmp_path: Path) -> None:
    spec_path = tmp_path / "test.spec"
    build_rpm.generate_spec_file("2.8.5", spec_path)
    assert spec_path.exists()
    content = spec_path.read_text(encoding="utf-8")
    assert "Version:        2.8.5" in content
    assert "Name:           mcubridge-gateway" in content


def test_create_source_tarball(tmp_path: Path) -> None:
    tarball = build_rpm.create_source_tarball("2.8.5", tmp_path)
    assert tarball.exists()
    assert tarball.name == "mcubridge-gateway-2.8.5.tar.gz"

    with tarfile.open(tarball, "r:gz") as tar:
        names = tar.getnames()
        assert "mcubridge-gateway-2.8.5/gateway.py" in names
        assert "mcubridge-gateway-2.8.5/mcubridge-gateway.service" in names
        assert "mcubridge-gateway-2.8.5/mcubridge/__init__.py" in names


def test_main_missing_rpmbuild(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_rpm.shutil, "which", lambda cmd: None)
    with pytest.raises(SystemExit) as exc:
        build_rpm.main(output_dir=tmp_path, build_dir=tmp_path / "rpmbuild")
    assert exc.value.code == 1


def test_main_rpmbuild_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_dir = tmp_path / "bin"
    output_dir.mkdir(parents=True, exist_ok=True)
    custom_build = tmp_path / "rpmbuild"

    monkeypatch.setattr(build_rpm.shutil, "which", lambda cmd: "/usr/bin/rpmbuild")

    def mock_run(cmd: list[str], check: bool = False) -> MagicMock:
        # Create a mock rpm file in rpmbuild/RPMS
        rpms_dir = custom_build / "RPMS" / "noarch"
        rpms_dir.mkdir(parents=True, exist_ok=True)
        (rpms_dir / "mcubridge-gateway-2.8.5-1.noarch.rpm").write_text("mock_rpm", encoding="utf-8")
        res = MagicMock()
        res.returncode = 0
        return res

    monkeypatch.setattr(build_rpm.subprocess, "run", mock_run)

    build_rpm.main(output_dir=output_dir, build_dir=custom_build, keep_build=False)

    rpm_dest = output_dir / "mcubridge-gateway-2.8.5-1.noarch.rpm"
    assert rpm_dest.exists()
    assert rpm_dest.read_text(encoding="utf-8") == "mock_rpm"
