#!/usr/bin/env python3
"""Generate derived dependency files from the runtime manifest."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from collections.abc import Sequence

import tomllib


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "requirements" / "runtime.toml"
REQUIREMENTS_PATH = ROOT / "requirements" / "runtime.txt"
MAKEFILE_PATH = ROOT / "openwrt-yun-bridge" / "Makefile"
BLOCK_START = "# AUTO-GENERATED RUNTIME DEPENDS BEGIN"
BLOCK_END = "# AUTO-GENERATED RUNTIME DEPENDS END"

# [MODIFICACIÓN FASE 3] Paquetes exclusivos del sistema (OpenWrt)
# Estos paquetes NO se incluirán en runtime.txt para evitar errores en pip install local
SYSTEM_ONLY_PACKAGES = {"uci"}


class ManifestError(RuntimeError):
    """Raised when the manifest file is missing or malformed."""


def load_manifest() -> list[dict]:
    if not MANIFEST_PATH.exists():
        raise ManifestError(f"Missing manifest: {MANIFEST_PATH}")

    data = tomllib.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
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
    # 1. Recolectar especificaciones crudas
    specs = {dep["pip"] for dep in deps if dep.get("pip")}

    # 2. Filtrar paquetes marcados como SYSTEM_ONLY
    filtered = {
        s for s in specs
        if not any(s.startswith(p) for p in SYSTEM_ONLY_PACKAGES)
    }
    return sorted(filtered)


def collect_openwrt_packages(deps: Sequence[dict]) -> list[str]:
    # Mantiene todos los paquetes para el Makefile (incluyendo uci)
    return [dep["openwrt"] for dep in deps if dep.get("openwrt")]


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


def format_openwrt_lines(tokens: Sequence[str]) -> list[str]:
    lines: list[str] = []
    for index, token in enumerate(tokens):
        suffix = " \\" if index < len(tokens) - 1 else ""
        lines.append(f"\t\t{token}{suffix}")
    return lines


def update_makefile(deps: Sequence[dict], *, dry_run: bool = False) -> bool:
    makefile_text = MAKEFILE_PATH.read_text(encoding="utf-8")
    if BLOCK_START not in makefile_text or BLOCK_END not in makefile_text:
        raise ManifestError(
            "Makefile is missing dependency markers; cannot inject dependencies"
        )
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


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit with status 1 if running would change any files",
    )
    parser.add_argument(
        "--print-openwrt",
        action="store_true",
        help="Print OpenWrt package names and exit",
    )
    parser.add_argument(
        "--print-pip",
        action="store_true",
        help="Print pip requirement specifiers and exit",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    deps = load_manifest()
    if args.print_openwrt:
        sys.stdout.write("\n".join(collect_openwrt_packages(deps)) + "\n")
    if args.print_pip:
        sys.stdout.write("\n".join(collect_pip_specs(deps)) + "\n")
    if args.print_openwrt or args.print_pip:
        return 0
    updated_requirements = write_requirements(deps, dry_run=args.check)
    updated_makefile = update_makefile(deps, dry_run=args.check)
    if args.check and (updated_requirements or updated_makefile):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
