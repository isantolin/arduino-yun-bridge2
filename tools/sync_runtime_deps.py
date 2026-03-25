#!/usr/bin/env python3
"""Generate derived dependency files from the runtime manifest."""

from __future__ import annotations

import sys
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import msgspec
import typer

app = typer.Typer(help="Generate derived dependency files from the runtime manifest.")

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


def load_manifest() -> list[dict]:
    if not MANIFEST_PATH.exists():
        raise ManifestError(f"Missing manifest: {MANIFEST_PATH}")

    data = msgspec.toml.decode(MANIFEST_PATH.read_text(encoding="utf-8"))
    entries = data.get("dependency")
    if not entries:
        raise ManifestError("Manifest must declare at least one dependency")
    normalized: list[dict] = []
    for entry in entries:
        openwrt = entry.get("openwrt", "").strip()
        pip_spec = entry.get("pip", "").strip()
        name = entry.get("name") or openwrt or "(unnamed)"
        normalized.append(
            {
                "name": name,
                "openwrt": openwrt,
                "pip": pip_spec,
            }
        )
    return normalized


def collect_pip_specs(deps: Sequence[dict]) -> list[str]:
    # Mantiene todo EXCEPTO los paquetes exclusivos de sistema (uci)
    specs = {dep["pip"] for dep in deps if dep.get("pip")}
    filtered = {s for s in specs if not any(s.startswith(p) for p in SYSTEM_ONLY_PACKAGES)}
    return sorted(filtered)


def collect_openwrt_packages(deps: Sequence[dict]) -> list[str]:
    # Mantiene todo EXCEPTO los paquetes exclusivos de construcción (jinja2, etc)
    # Esto asegura que el APK sea ultra-lean.
    return [
        dep["openwrt"]
        for dep in deps
        if dep.get("openwrt") and dep["name"] not in BUILD_ONLY_PACKAGES
    ]


def write_requirements(deps: Sequence[dict], *, dry_run: bool = False) -> bool:
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


def update_pyproject(deps: Sequence[dict], *, dry_run: bool = False) -> bool:
    if not PYPROJECT_PATH.exists():
        return False
    
    # Collect only runtime dependencies for project.dependencies
    runtime_pip_specs = sorted([
        dep["pip"] for dep in deps 
        if dep.get("pip") and dep["name"] not in BUILD_ONLY_PACKAGES 
        and not any(dep["pip"].startswith(p) for p in SYSTEM_ONLY_PACKAGES)
    ])
    
    content = PYPROJECT_PATH.read_text(encoding="utf-8")
    
    # Find dependencies list
    pattern = r'dependencies = \[\s*([\s\S]*?)\s*\]'
    
    formatted_deps = "dependencies = [\n" + ",\n".join(f'    "{s}"' for s in runtime_pip_specs) + "\n]"
    
    new_content = re.sub(pattern, formatted_deps, content, count=1)
    
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


def update_makefile(deps: Sequence[dict], *, dry_run: bool = False) -> bool:
    makefile_text = MAKEFILE_PATH.read_text(encoding="utf-8")
    if BLOCK_START not in makefile_text or BLOCK_END not in makefile_text:
        raise ManifestError("Makefile is missing dependency markers; cannot inject dependencies")
    tokens = [f"+{pkg}" for pkg in collect_openwrt_packages(deps)]
    if tokens:
        block_lines = ["\tDEPENDS+= \\"]
        block_lines.extend(format_openwrt_lines(tokens))
    else:
        block_lines = ["\tDEPENDS+="]
    rendered_block = "\n".join(block_lines)
    new_text = []
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


@app.command()
def main(
    check: Annotated[
        bool,
        typer.Option("--check", help="Exit with status 1 if running would change any files"),
    ] = False,
    print_openwrt: Annotated[
        bool, typer.Option("--print-openwrt", help="Print OpenWrt package names and exit")
    ] = False,
    print_pip: Annotated[bool, typer.Option("--print-pip", help="Print pip requirement specifiers and exit")] = False,
) -> None:
    deps = load_manifest()
    if print_openwrt:
        sys.stdout.write("\n".join(collect_openwrt_packages(deps)) + "\n")
        raise typer.Exit()
    if print_pip:
        sys.stdout.write("\n".join(collect_pip_specs(deps)) + "\n")
        raise typer.Exit()

    updated_requirements = write_requirements(deps, dry_run=check)
    updated_makefile = update_makefile(deps, dry_run=check)
    updated_pyproject = update_pyproject(deps, dry_run=check)

    if check and (updated_requirements or updated_makefile or updated_pyproject):
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
