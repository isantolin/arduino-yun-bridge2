#!/usr/bin/env python3
"""[MIL-SPEC/SIL-2] Populate OpenWrt feed directory with symlinks to canonical package sources."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    help="Populate destination feed directory with symlinks to canonical packages.",
    add_completion=False,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEST = REPO_ROOT / "feeds"
MANAGED_PACKAGES = ("luci-app-mcubridge", "mcubridge", "mcubridge-gateway")


@app.command()
def main(
    dest: Annotated[Path, typer.Option("--dest", help="Destination feed directory")] = DEFAULT_DEST,
    clean: Annotated[
        bool, typer.Option("--clean", help="Remove existing managed package entries before linking")
    ] = False,
) -> None:
    dest.mkdir(parents=True, exist_ok=True)

    if clean:
        print(f"[sync-feed] Cleaning managed packages in {dest}")
        for pkg_name in MANAGED_PACKAGES:
            target = dest / pkg_name
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                import shutil

                shutil.rmtree(target)

    for pkg_name in MANAGED_PACKAGES:
        src = REPO_ROOT / pkg_name
        if not src.is_dir():
            raise FileNotFoundError(f"[sync-feed] Source directory {src} not found")

        pkg_dest = dest / pkg_name
        if pkg_dest.is_symlink() or pkg_dest.is_file():
            pkg_dest.unlink()
        elif pkg_dest.is_dir():
            import shutil

            shutil.rmtree(pkg_dest)

        rel_target = os.path.relpath(src, dest)
        pkg_dest.symlink_to(rel_target)
        print(f"[sync-feed] Linked {pkg_name} -> {rel_target}")


if __name__ == "__main__":
    app()
