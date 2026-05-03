#!/usr/bin/env python3
"""Generate derived dependency files from the runtime manifest."""

from __future__ import annotations

import argparse
import sys
import urllib.request
import urllib.error
from collections.abc import Sequence
from pathlib import Path
from typing import TypedDict

import msgspec

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "requirements" / "runtime.toml"
REQUIREMENTS_PATH = ROOT / "requirements" / "runtime.txt"
PYPROJECT_PATH = ROOT / "pyproject.toml"
MAKEFILE_PATH = ROOT / "mcubridge" / "Makefile"
BLOCK_START = "# AUTO-GENERATED RUNTIME DEPENDS BEGIN"
BLOCK_END = "# AUTO-GENERATED RUNTIME DEPENDS END"

# --- [FILTRADO INTELIGENTE DE DEPENDENCIAS] ---

# uci: Solo en OpenWrt (Makefile), no en pip (runtime.txt) para evitar errores locales.
SYSTEM_ONLY_PACKAGES = {"uci"}

# jinja2, nanopb, grpcio-tools, xxd: Solo en pip (Dev/CI), no en el APK de OpenWrt para ahorrar Flash.
BUILD_ONLY_PACKAGES = {"jinja2", "nanopb", "grpcio-tools", "xxd"}


class ManifestError(RuntimeError):
    """Raised when the manifest file is missing or malformed."""


class _DepEntry(TypedDict):
    name: str
    openwrt: str
    pip: str


def load_manifest() -> list[_DepEntry]:
    if not MANIFEST_PATH.exists():
        raise ManifestError(f"Missing manifest: {MANIFEST_PATH}")

    data = msgspec.toml.decode(MANIFEST_PATH.read_text(encoding="utf-8"))
    entries = data.get("dependency")
    if not entries:
        raise ManifestError("Manifest must declare at least one dependency")
    normalized: list[_DepEntry] = []
    for entry in entries:
        openwrt = entry.get("openwrt", "").strip()
        pip_spec = entry.get("pip", "").strip()
        name = entry.get("name") or openwrt or "(unnamed)"
        normalized.append(
            _DepEntry(
                name=name,
                openwrt=openwrt,
                pip=pip_spec,
            )
        )
    return normalized


def collect_pip_specs(deps: Sequence[_DepEntry]) -> list[str]:
    # Mantiene todo EXCEPTO los paquetes exclusivos de sistema (uci)
    specs = {dep["pip"] for dep in deps if dep.get("pip")}
    filtered = {
        s for s in specs if not any(s.startswith(p) for p in SYSTEM_ONLY_PACKAGES)
    }
    return sorted(filtered)


def collect_openwrt_packages(deps: Sequence[_DepEntry]) -> list[str]:
    # Mantiene todo EXCEPTO los paquetes exclusivos de construcción (jinja2, etc)
    # Esto asegura que el APK sea ultra-lean.
    return [
        dep["openwrt"]
        for dep in deps
        if dep.get("openwrt") and dep["name"] not in BUILD_ONLY_PACKAGES
    ]


def write_requirements(deps: Sequence[_DepEntry], *, dry_run: bool = False) -> bool:
    pip_specs = collect_pip_specs(deps)
    content = ["# Generated via tools/sync_runtime_deps.py; do not edit."]
    content.extend(pip_specs)
    new_text = "\n".join(content) + "\n"
    if REQUIREMENTS_PATH.exists():
        existing = REQUIREMENTS_PATH.read_text(encoding="utf-8")
        if existing == new_text:
            return False
    if not dry_run:
        REQUIREMENTS_PATH.write_text(new_text, encoding="utf-8")
    return True


def update_pyproject(deps: Sequence[_DepEntry], *, dry_run: bool = False) -> bool:
    if not PYPROJECT_PATH.exists():
        return False

    # Collect only runtime dependencies for project.dependencies
    runtime_pip_specs = sorted(
        [
            dep["pip"]
            for dep in deps
            if (
                dep.get("pip")
                and dep["name"] not in BUILD_ONLY_PACKAGES
                and not any(dep["pip"].startswith(p) for p in SYSTEM_ONLY_PACKAGES)
            )  # noqa: W503
        ]
    )

    content = PYPROJECT_PATH.read_text(encoding="utf-8")

    # Robust replacement of dependencies block
    lines = content.splitlines()
    new_lines: list[str] = []
    in_dependencies = False
    replaced = False

    for line in lines:
        if not replaced and line.strip() == "dependencies = [":
            in_dependencies = True
            new_lines.append(line)
            for spec in runtime_pip_specs:
                new_lines.append(f'    "{spec}",')
            # Remove trailing comma from last dependency for strictly valid TOML if preferred,
            # though most parsers handle it. Ruff likes it.
            replaced = True
            continue

        if in_dependencies:
            if line.strip() == "]":
                in_dependencies = False
                new_lines.append(line)
            continue

        new_lines.append(line)

    new_content = "\n".join(new_lines) + "\n"

    if new_content == content:
        return False

    if not dry_run:
        PYPROJECT_PATH.write_text(new_content, encoding="utf-8")
    return True


def format_openwrt_lines(tokens: Sequence[str]) -> list[str]:
    lines: list[str] = []
    for index, token in enumerate(tokens):
        suffix = " \\" if index < len(tokens) - 1 else ""
        lines.append(f"\t\t{token}{suffix}")
    return lines


def update_makefile(deps: Sequence[_DepEntry], *, dry_run: bool = False) -> bool:
    makefile_text = MAKEFILE_PATH.read_text(encoding="utf-8")
    if BLOCK_START not in makefile_text or BLOCK_END not in makefile_text:
        raise ManifestError(
            "Makefile is missing dependency markers; cannot inject dependencies"
        )
    tokens = [f"{pkg}" for pkg in collect_openwrt_packages(deps)]
    if tokens:
        block_lines = ["\tDEPENDS+= \\"]
        block_lines.extend(format_openwrt_lines(tokens))
    else:
        block_lines = ["\tDEPENDS+="]
    rendered_block = "\n".join(block_lines)
    new_text: list[str] = []
    in_block = False
    for line in makefile_text.splitlines():
        if BLOCK_START in line:
            in_block = True
            new_text.append(line)
            new_text.append(rendered_block)
            continue
        if BLOCK_END in line:
            in_block = False
            new_text.append(line)
            continue
        if not in_block:
            new_text.append(line)
    updated = "\n".join(new_text) + "\n"
    if updated == makefile_text:
        return False
    if not dry_run:
        MAKEFILE_PATH.write_text(updated, encoding="utf-8")
    return True


def _parse_pip_spec(spec: str) -> tuple[str, str]:
    """Extract (package_name, pinned_version) from a pip spec like 'foo==1.2.3'."""
    if "==" not in spec:
        return spec, ""
    # Handle extras: 'typer[all]==0.24.1' -> 'typer', '0.24.1'
    name_part, version = spec.split("==", 1)
    name = name_part.split("[")[0].strip()
    return name, version.strip()


def _fetch_latest_version(package_name: str) -> str | None:
    """Query PyPI JSON API for the latest release version."""
    url = f"https://pypi.org/pypi/{package_name}/json"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
            data = msgspec.json.decode(resp.read())
            return data["info"]["version"]
    except (urllib.error.URLError, ValueError, KeyError, msgspec.DecodeError):
        return None


def check_latest_versions(deps: Sequence[_DepEntry]) -> list[tuple[str, str, str]]:
    """Return list of (package, pinned, latest) for outdated packages."""
    outdated: list[tuple[str, str, str]] = []
    pip_specs = [dep["pip"] for dep in deps if dep.get("pip")]
    for spec in pip_specs:
        name, pinned = _parse_pip_spec(spec)
        if not pinned:
            continue
        latest = _fetch_latest_version(name)
        if latest and latest != pinned:
            outdated.append((name, pinned, latest))
    return outdated


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Generate derived dependency files from the runtime manifest."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        default=False,
        help="Exit with status 1 if running would change any files",
    )
    parser.add_argument(
        "--check-latest",
        action="store_true",
        default=False,
        help="Query PyPI and warn about outdated pinned versions",
    )
    parser.add_argument(
        "--print-openwrt",
        action="store_true",
        default=False,
        help="Print OpenWrt package names and exit",
    )
    parser.add_argument(
        "--print-pip",
        action="store_true",
        default=False,
        help="Print pip requirement specifiers and exit",
    )
    args = parser.parse_args(argv)
    check: bool = args.check
    check_latest: bool = args.check_latest
    print_openwrt: bool = args.print_openwrt
    print_pip: bool = args.print_pip
    deps = load_manifest()
    if print_openwrt:
        sys.stdout.write("\n".join(collect_openwrt_packages(deps)) + "\n")
        raise SystemExit(0)
    if print_pip:
        sys.stdout.write("\n".join(collect_pip_specs(deps)) + "\n")
        raise SystemExit(0)

    updated_requirements = write_requirements(deps, dry_run=check)
    updated_makefile = update_makefile(deps, dry_run=check)
    updated_pyproject = update_pyproject(deps, dry_run=check)

    fail = False
    if check and (updated_requirements or updated_makefile or updated_pyproject):
        fail = True

    if check_latest:
        outdated = check_latest_versions(deps)
        if outdated:
            print("Outdated dependencies:")
            for name, pinned, latest in outdated:
                print(f"  {name}: {pinned} -> {latest}")
            fail = True
        else:
            print("All dependencies are up to date.")

    if fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
