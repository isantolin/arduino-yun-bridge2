#!/usr/bin/env python3
"""Synchronize runtime dependencies between runtime.toml and mcubridge/Makefile."""

from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Annotated, TypeVar

import msgspec
import typer

app = typer.Typer(add_completion=False)

# --- Configuration ---
REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_TOML = REPO_ROOT / "requirements" / "runtime.toml"
MAKEFILE = REPO_ROOT / "mcubridge" / "Makefile"

BLOCK_START = "# --- RUNTIME DEPENDENCIES START ---"
BLOCK_END = "# --- RUNTIME DEPENDENCIES END ---"

_T = TypeVar("_T")


class ManifestError(RuntimeError):
    """Raised when the dependency manifest is inconsistent."""


class _DepEntry(msgspec.Struct):
    name: str
    version: str
    apk: str | None = None


def load_deps() -> list[_DepEntry]:
    """Load dependencies from runtime.toml."""
    try:
        data = msgspec.toml.decode(RUNTIME_TOML.read_text())
        deps_raw = data.get("dependencies", [])
        return [msgspec.convert(d, _DepEntry) for d in deps_raw]
    except (msgspec.MsgspecError, OSError) as e:
        sys.stderr.write(f"Error: Failed to load {RUNTIME_TOML}: {e}\n")
        raise typer.Exit(code=1) from e


def get_pypi_version(package_name: str) -> str | None:
    """Fetch the latest version of a package from PyPI JSON API."""
    url = f"https://pypi.org/pypi/{package_name}/json"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
            data = msgspec.json.decode(resp.read())
            return str(data["info"]["version"])
    except (urllib.error.URLError, msgspec.MsgspecError, OSError, TimeoutError, KeyError):
        return None


def check_latest_versions(deps: Sequence[_DepEntry]) -> list[tuple[str, str, str]]:
    """Return list of (package, pinned, latest) for outdated packages."""
    outdated: list[tuple[str, str, str]] = []
    for dep in deps:
        name = dep.name
        pinned = dep.version
        if not pinned:
            continue
        latest = get_pypi_version(name)
        if latest and latest != pinned:
            outdated.append((name, pinned, latest))
    return outdated


def collect_pip_specs(deps: Iterable[_DepEntry]) -> list[str]:
    """Generate pip-compatible requirement strings."""
    return [f"{d.name}=={d.version}" for d in deps if d.version]


def collect_apk_names(deps: Iterable[_DepEntry]) -> list[str]:
    """Generate APK package list for OpenWrt."""
    return [d.apk for d in deps if d.apk]


def write_requirements(deps: Iterable[_DepEntry], dry_run: bool = False) -> bool:
    """Update requirements/runtime.txt."""
    output_path = REPO_ROOT / "requirements" / "runtime.txt"
    lines = collect_pip_specs(deps)
    new_content = "\n".join(lines) + "\n"

    if dry_run:
        if not output_path.exists() or output_path.read_text() != new_content:
            return True
        return False

    output_path.write_text(new_content)
    return True


def update_makefile(deps: Iterable[_DepEntry], dry_run: bool = False) -> bool:
    """Update PKG_BUILD_DEPENDS in mcubridge/Makefile."""
    apk_list = collect_apk_names(deps)
    apk_line = " ".join(apk_list)
    new_block = f"{BLOCK_START}\nPKG_BUILD_DEPENDS:={apk_line}\n{BLOCK_END}"

    makefile_text = MAKEFILE.read_text()
    if BLOCK_START not in makefile_text or BLOCK_END not in makefile_text:
        raise ManifestError(
            f"Could not find dependency block markers in {MAKEFILE}"
        )

    pattern = re.compile(f"{re.escape(BLOCK_START)}.*?{re.escape(BLOCK_END)}", re.DOTALL)
    updated_text = pattern.sub(new_block, makefile_text)

    if dry_run:
        return updated_text != makefile_text

    MAKEFILE.write_text(updated_text)
    return True


@app.command()
def sync(
    check: Annotated[bool, typer.Option("--check", help="Only check for changes")] = False,
    update_pypi: Annotated[bool, typer.Option("--update", help="Check PyPI for newer versions")] = False,
) -> None:
    """Synchronize runtime dependencies across the project."""
    deps = load_deps()

    if update_pypi:
        typer.echo("Checking PyPI for updates...")
        outdated = check_latest_versions(deps)
        if outdated:
            typer.echo("\nOutdated packages found:")
            for name, pinned_ver, latest_ver in outdated:
                typer.echo(f"  {name}: {pinned_ver} -> {latest_ver}")
            typer.echo("\nPlease update requirements/runtime.toml manually.")
        else:
            typer.echo("All packages are up to date.")

    changed_req = write_requirements(deps, dry_run=check)
    changed_make = update_makefile(deps, dry_run=check)

    if check:
        if changed_req or changed_make:
            typer.echo("Changes detected. Run without --check to synchronize.")
            raise typer.Exit(code=1)
        typer.echo("Synchronized.")
    else:
        if changed_req or changed_make:
            typer.echo("Synchronized successfully.")
        else:
            typer.echo("Already synchronized.")


if __name__ == "__main__":
    app()
