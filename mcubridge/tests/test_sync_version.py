"""Tests for explicit package-version synchronization."""

from pathlib import Path

from tools import sync_version


def test_sync_metadata_updates_only_version_tokens(tmp_path: Path) -> None:
    files = {
        "pyproject.toml": '[project]\nversion = "1.0.0"\ndescription = "keep"\n',
        "mcubridge/mcubridge/__init__.py": '__version__ = "1.0.0"\nOTHER = "1.0.0"\n',
        "mcubridge/Makefile": "PKG_VERSION:=1.0.0\nOTHER:=keep\n",
        "mcubridge-gateway/Makefile": "PKG_VERSION:=1.0.0\n",
        "luci-app-mcubridge/Makefile": "PKG_VERSION:=1.0.0\n",
        "mcubridge-library-arduino/library.properties": "version=1.0.0\nname=Bridge\n",
    }
    for relative_path, content in files.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    changed = sync_version.sync_metadata("2.3.4", tmp_path)

    assert {path.relative_to(tmp_path).as_posix() for path in changed} == set(files)
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == (
        '[project]\nversion = "2.3.4"\ndescription = "keep"\n'
    )
    assert (tmp_path / "mcubridge/mcubridge/__init__.py").read_text(encoding="utf-8") == (
        '__version__ = "2.3.4"\nOTHER = "1.0.0"\n'
    )
