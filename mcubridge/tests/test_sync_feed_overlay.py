"""Unit tests for sync_feed_overlay tool (SIL-2 / Rule 11 / Rule 18)."""

from __future__ import annotations
from pathlib import Path
import pytest
from tools.ci import sync_feed_overlay


def test_sync_feed_overlay_creates_valid_symlinks(tmp_path: Path) -> None:
    dest = tmp_path / "feeds"
    sync_feed_overlay.main(dest=dest, clean=True)

    for pkg_name in sync_feed_overlay.MANAGED_PACKAGES:
        target = dest / pkg_name
        assert target.is_symlink()
        assert target.resolve().is_dir()
        assert target.resolve() == (sync_feed_overlay.REPO_ROOT / pkg_name).resolve()


def test_sync_feed_overlay_clean_removes_existing(tmp_path: Path) -> None:
    dest = tmp_path / "feeds"
    dest.mkdir(parents=True)
    stale_file = dest / "mcubridge"
    stale_file.write_text("stale content", encoding="utf-8")

    sync_feed_overlay.main(dest=dest, clean=True)

    assert (dest / "mcubridge").is_symlink()
    assert (dest / "mcubridge").resolve().is_dir()


def test_sync_feed_overlay_missing_source_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "feeds"
    monkeypatch.setattr(sync_feed_overlay, "MANAGED_PACKAGES", ("non_existent_package_xyz",))

    with pytest.raises(FileNotFoundError, match="Source directory .* not found"):
        sync_feed_overlay.main(dest=dest, clean=False)
