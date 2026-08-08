#!/usr/bin/env python3
"""Generate derived dependency files from the runtime manifest."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypedDict, cast

import tomllib
from distlib.locators import PyPIJSONLocator  # type: ignore[import-untyped]
from graphlib import TopologicalSorter
from packaging.requirements import Requirement
from packaging.version import parse as parse_version

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "requirements" / "runtime.toml"
REQUIREMENTS_PATH = ROOT / "requirements" / "runtime.txt"
PYPROJECT_PATH = ROOT / "pyproject.toml"
MAKEFILE_PATH = ROOT / "mcubridge" / "Makefile"
GATEWAY_REQUIREMENTS_PATH = ROOT / "mcubridge-gateway" / "requirements.txt"
GATEWAY_MAKEFILE_PATH = ROOT / "mcubridge-gateway" / "Makefile"
FEEDS_DIR = ROOT / "feeds"
BLOCK_START = "# AUTO-GENERATED RUNTIME DEPENDS BEGIN"
BLOCK_END = "# AUTO-GENERATED RUNTIME DEPENDS END"

# --- [FILTRADO INTELIGENTE DE DEPENDENCIAS] ---

# uci: Solo en OpenWrt (Makefile), no en pip (runtime.txt) para evitar errores locales.
SYSTEM_ONLY_PACKAGES = {"uci"}

# Dev/CI host build-only packages (excluded from OpenWrt MPU package dependencies).
BUILD_ONLY_PACKAGES = {"jinja2", "nanopb", "grpcio-tools", "xxd", "black"}


class ManifestError(RuntimeError):
    """Raised when the manifest file is missing or malformed."""


class _DepEntry(TypedDict):
    name: str
    openwrt: str
    pip: str
    check_latest: bool
    gateway: bool


def sort_dependencies_topologically(deps: Sequence[_DepEntry]) -> list[_DepEntry]:
    """Sorts dependencies in topological order using standard graphlib."""
    ts: TopologicalSorter[str] = TopologicalSorter()
    dep_map = {dep["name"]: dep for dep in deps}

    for dep in deps:
        ts.add(dep["name"])

    return [dep_map[name] for name in ts.static_order() if name in dep_map]


def load_manifest() -> list[_DepEntry]:
    if not MANIFEST_PATH.exists():
        raise ManifestError(f"Missing manifest: {MANIFEST_PATH}")

    data = tomllib.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
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
                check_latest=bool(entry.get("check_latest", True)),
                gateway=bool(entry.get("gateway", False)),
            )
        )
    return sort_dependencies_topologically(normalized)


def collect_pip_specs(deps: Sequence[_DepEntry]) -> list[str]:
    # Mantiene todo EXCEPTO los paquetes exclusivos de sistema (uci)
    specs = {dep["pip"] for dep in deps if dep.get("pip")}
    filtered = {s for s in specs if not any(s.startswith(p) for p in SYSTEM_ONLY_PACKAGES)}
    return sorted(filtered)


def collect_openwrt_packages(deps: Sequence[_DepEntry]) -> list[str]:
    # Mantiene todo EXCEPTO los paquetes exclusivos de construcción (jinja2, etc)
    # Esto asegura que el APK sea ultra-lean.
    return [dep["openwrt"] for dep in deps if dep.get("openwrt") and dep["name"] not in BUILD_ONLY_PACKAGES]


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


def write_gateway_requirements(deps: Sequence[_DepEntry], *, dry_run: bool = False) -> bool:
    gateway_deps = [dep for dep in deps if dep.get("gateway")]
    pip_specs = collect_pip_specs(gateway_deps)
    content = ["# Generated via tools/sync_runtime_deps.py; do not edit."]
    content.extend(pip_specs)
    new_text = "\n".join(content) + "\n"
    if GATEWAY_REQUIREMENTS_PATH.exists():
        existing = GATEWAY_REQUIREMENTS_PATH.read_text(encoding="utf-8")
        if existing == new_text:
            return False
    if not dry_run:
        GATEWAY_REQUIREMENTS_PATH.write_text(new_text, encoding="utf-8")
    return True


def update_pyproject(deps: Sequence[_DepEntry], *, dry_run: bool = False) -> bool:
    if not PYPROJECT_PATH.exists():
        return False

    runtime_pip_specs = sorted(
        [
            dep["pip"]
            for dep in deps
            if (dep.get("pip") and not any(dep["pip"].startswith(p) for p in SYSTEM_ONLY_PACKAGES))
        ]
    )

    content = PYPROJECT_PATH.read_text(encoding="utf-8")
    formatted_deps = "dependencies = [\n" + "\n".join(f'    "{spec}",' for spec in runtime_pip_specs) + "\n]"
    start_marker = "dependencies = ["
    if start_marker in content:
        start_idx = content.index(start_marker)
        end_idx = content.find("]", start_idx)
        if end_idx != -1:
            new_content = content[:start_idx] + formatted_deps + content[end_idx + 1 :]
        else:
            new_content = content
    else:
        new_content = content

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


def _replace_block(text: str, start_marker: str, end_marker: str, replacement: str) -> str:
    start_pos = text.find(start_marker)
    end_pos = text.find(end_marker)
    if start_pos == -1 or end_pos == -1 or end_pos < start_pos:
        return text
    start_cut = start_pos + len(start_marker)
    if text[start_cut : start_cut + 1] == "\n":
        start_cut += 1
    return text[:start_cut] + replacement + "\n" + text[end_pos:]


def update_makefile(deps: Sequence[_DepEntry], *, dry_run: bool = False) -> bool:
    makefile_text = MAKEFILE_PATH.read_text(encoding="utf-8")
    if BLOCK_START not in makefile_text or BLOCK_END not in makefile_text:
        raise ManifestError("Makefile is missing dependency markers; cannot inject dependencies")
    tokens = [f"{pkg}" for pkg in collect_openwrt_packages(deps)]
    if tokens:
        block_lines = ["\tDEPENDS+= \\"]
        block_lines.extend(format_openwrt_lines(tokens))
    else:
        block_lines = ["\tDEPENDS+="]
    rendered_block = "\n".join(block_lines)
    updated = _replace_block(makefile_text, BLOCK_START, BLOCK_END, rendered_block)
    if updated == makefile_text:
        return False
    if not dry_run:
        MAKEFILE_PATH.write_text(updated, encoding="utf-8")
    return True


def update_gateway_makefile(deps: Sequence[_DepEntry], *, dry_run: bool = False) -> bool:
    if not GATEWAY_MAKEFILE_PATH.exists():
        return False
    makefile_text = GATEWAY_MAKEFILE_PATH.read_text(encoding="utf-8")
    if BLOCK_START not in makefile_text or BLOCK_END not in makefile_text:
        raise ManifestError("Gateway Makefile is missing dependency markers")
    gateway_deps = [dep for dep in deps if dep.get("gateway")]
    tokens = [f"{pkg}" for pkg in collect_openwrt_packages(gateway_deps)]
    if tokens:
        block_lines = ["\tDEPENDS+= \\"]
        block_lines.extend(format_openwrt_lines(tokens))
    else:
        block_lines = ["\tDEPENDS+="]
    rendered_block = "\n".join(block_lines)
    updated = _replace_block(makefile_text, BLOCK_START, BLOCK_END, rendered_block)
    if updated == makefile_text:
        return False
    if not dry_run:
        GATEWAY_MAKEFILE_PATH.write_text(updated, encoding="utf-8")
    return True


def _parse_pip_spec(spec: str) -> tuple[str, str]:
    """Extract (package_name, pinned_version) from a pip spec using packaging library."""
    if not spec:
        return "", ""
    try:
        req = Requirement(spec)
        version = ""
        for specifier in req.specifier:
            if specifier.operator == "==":
                version = specifier.version
                break
        return req.name, version
    except Exception:
        if "==" in spec:
            name_part, ver = spec.split("==", 1)
            return name_part.split("[")[0].strip(), ver.strip()
        return spec.split("[")[0].strip(), ""


def _fetch_latest_version(package_name: str, *, include_prerelease: bool = False) -> str | None:
    """Fetch latest package version from PyPI using distlib PyPIJSONLocator and packaging.version sorting."""
    try:
        locator: Any = cast(Any, PyPIJSONLocator)("https://pypi.org/pypi")
        project_data: dict[str, Any] = cast(dict[str, Any], locator.get_project(package_name))
        if not project_data:
            return None
        parsed_versions: list[tuple[Any, str]] = []
        keys_list: list[str] = list(project_data.keys())
        for ver_str in keys_list:
            if ver_str in ("urls", "digests"):
                continue
            try:
                v = parse_version(str(ver_str))
                if include_prerelease or not v.is_prerelease:
                    parsed_versions.append((v, str(ver_str)))
            except Exception:
                continue
        if parsed_versions:
            parsed_versions.sort(key=lambda item: item[0])
            return parsed_versions[-1][1]
        return None
    except Exception:
        return None


def check_latest_versions(deps: Sequence[_DepEntry]) -> list[tuple[str, str, str]]:
    """Return list of (package, pinned, latest) for outdated packages using packaging.version comparison."""
    outdated: list[tuple[str, str, str]] = []
    pip_specs = [(dep["pip"], dep["check_latest"]) for dep in deps if dep.get("pip")]
    for spec, should_check_latest in pip_specs:
        if not should_check_latest:
            continue
        name, pinned = _parse_pip_spec(spec)
        if not pinned:
            continue
        try:
            pinned_v = parse_version(pinned)
            is_prerelease = pinned_v.is_prerelease
        except Exception:
            is_prerelease = False
            pinned_v = None

        latest_str = _fetch_latest_version(name, include_prerelease=is_prerelease)
        if latest_str:
            try:
                latest_v = parse_version(latest_str)
                if pinned_v and latest_v > pinned_v:
                    outdated.append((name, pinned, latest_str))
            except Exception:
                if latest_str != pinned:
                    outdated.append((name, pinned, latest_str))
    return outdated


def _to_apk_version(version: str) -> str:
    """Convert Python pre-release notation to APK (Alpine) version notation using packaging.version."""
    try:
        v = parse_version(version)
        base = f"{v.major}.{v.minor}.{v.micro}"
        if v.pre:
            phase, num = v.pre
            phase_map = {"a": "_alpha", "b": "_beta", "rc": "_rc"}
            base += f"{phase_map.get(phase, f'_{phase}')}{num}"
        if v.dev is not None:
            base += f"_pre{v.dev}"
        return base
    except Exception:
        return version


def update_feeds(deps: Sequence[_DepEntry], *, dry_run: bool = False) -> bool:
    if not FEEDS_DIR.exists():
        return False

    any_updated = False
    for dep in deps:
        openwrt_pkg = dep.get("openwrt", "")
        if not openwrt_pkg or not openwrt_pkg.startswith("python3-"):
            continue

        pip_name, version = _parse_pip_spec(dep.get("pip", ""))
        if not version:
            continue

        makefile = FEEDS_DIR / openwrt_pkg / "Makefile"
        if not makefile.exists():
            continue

        content = makefile.read_text(encoding="utf-8")

        # Packages that include pypi.mk AND declare PYTHON3_PKG_WHEEL_VERSION use
        # a split notation: PKG_VERSION is APK notation (for apk mkpkg), while
        # PYTHON3_PKG_WHEEL_VERSION stays in Python notation (for wheel glob matching).
        # Packages that include pypi.mk WITHOUT the wheel override use Python notation.
        # Packages without pypi.mk use APK notation with explicit source/builddir.
        uses_pypi_mk = bool(re.search(r"^\s*include\b.*\bpypi\.mk\b", content, re.MULTILINE))
        is_prerelease = _to_apk_version(version) != version

        if uses_pypi_mk and not is_prerelease:
            # Standard pypi.mk package without pre-release: Python notation throughout.
            pkg_version = version
            new_content = re.sub(r"PKG_VERSION:=[^\n]+", f"PKG_VERSION:={pkg_version}", content)
            new_content = re.sub(r"PYTHON3_PKG_WHEEL_VERSION:=[^\n]+\n?", "", new_content)
            new_content = re.sub(r"PKG_SOURCE:=[^\n]+\n?", "", new_content)
            new_content = re.sub(r"PKG_BUILD_DIR:=[^\n]+\n?", "", new_content)
            new_content = re.sub(r"PYPI_SOURCE_NAME:=[^\n]+\n?", "", new_content)
            new_content = re.sub(r"PYPI_SOURCE_NAME_VERSION:=[^\n]+\n?", "", new_content)
        elif uses_pypi_mk and is_prerelease:
            # Pre-release pypi.mk package: APK version for PKG_VERSION, Python version for wheel & PyPI source.
            #
            # pypi.mk derives PKG_SOURCE (via ?=, so an earlier explicit value wins)
            # and PKG_BUILD_DIR (via :=, so pypi.mk's own assignment always wins,
            # regardless of anything set before its `include`) as
            # "$(PYPI_SOURCE_NAME)-$(PKG_VERSION)". The real PyPI sdist is named
            # "$(PYPI_SOURCE_NAME)-<raw version>.tar.gz" and extracts into a matching
            # directory (raw Python version, no APK "_rc" suffix), so PKG_SOURCE and
            # PKG_BUILD_DIR must be re-pinned to that raw name/version explicitly
            # (PKG_BUILD_DIR *after* pypi.mk's `include` line, otherwise pypi.mk
            # silently overwrites it and the package builds from a directory that
            # was never actually extracted into).
            #
            # IMPORTANT: PYPI_SOURCE_NAME must NOT be overridden with the version
            # baked in (e.g. "protobuf-7.36.0rc1"). python3-package.mk derives
            # PYTHON3_PKG_WHEEL_NAME from PYPI_SOURCE_NAME when set, so baking the
            # version into it produces a bogus wheel glob (e.g.
            # "protobuf_7.36.0rc1-7.36.0rc1-*.whl" instead of the real
            # "protobuf-7.36.0rc1-*.whl"), which fails at the `installer` step even
            # though the extension compiled successfully. Leave PYPI_SOURCE_NAME
            # unset so it defaults to the bare PYPI_NAME (e.g. "protobuf").
            pkg_version = _to_apk_version(version)
            pypi_source_name = f"{pip_name}-{version}"
            new_content = re.sub(r"PKG_VERSION:=[^\n]+", f"PKG_VERSION:={pkg_version}", content)
            new_content = re.sub(r"[ \t]*PYPI_SOURCE_NAME:=[^\n]+\n", "", new_content)
            if "PKG_SOURCE:=" in new_content:
                new_content = re.sub(
                    r"PKG_SOURCE:=[^\n]+",
                    f"PKG_SOURCE:={pypi_source_name}.tar.gz",
                    new_content,
                )
            else:
                new_content = re.sub(
                    r"(PYPI_NAME:=[^\n]+\n)",
                    f"\\1PKG_SOURCE:={pypi_source_name}.tar.gz\n",
                    new_content,
                )
            if "PYTHON3_PKG_WHEEL_VERSION:=" in new_content:
                new_content = re.sub(
                    r"PYTHON3_PKG_WHEEL_VERSION:=[^\n]+",
                    f"PYTHON3_PKG_WHEEL_VERSION:={version}",
                    new_content,
                )
            else:
                new_content = re.sub(
                    r"(PKG_SOURCE:=[^\n]+\n)",
                    f"\\1PYTHON3_PKG_WHEEL_VERSION:={version}\n",
                    new_content,
                )
            # Must come after pypi.mk's `include` (":=" assignment there would
            # otherwise clobber a value set earlier in the file).
            build_dir_line = f"PKG_BUILD_DIR:=$(BUILD_DIR)/pypi/{pypi_source_name}\n"
            pypi_mk_include = re.compile(r"(^\s*include\b.*\bpypi\.mk\b[^\n]*\n)", re.MULTILINE)
            if "PKG_BUILD_DIR:=" in new_content:
                new_content = re.sub(r"[ \t]*PKG_BUILD_DIR:=[^\n]+\n", "", new_content)
            new_content = pypi_mk_include.sub(lambda m: m.group(1) + build_dir_line, new_content, count=1)
        else:
            # Non-pypi.mk package: APK notation for PKG_VERSION, Python for source/builddir.
            pkg_version = _to_apk_version(version)
            new_content = re.sub(r"PKG_VERSION:=[^\n]+", f"PKG_VERSION:={pkg_version}", content)
            new_content = re.sub(
                r"PKG_SOURCE:=[^\n]+\.tar\.gz",
                f"PKG_SOURCE:={pip_name}-{version}.tar.gz",
                new_content,
            )
            new_content = re.sub(
                r"PKG_BUILD_DIR:=[^\n]+",
                f"PKG_BUILD_DIR:=$(BUILD_DIR)/pypi/{pip_name}-{version}",
                new_content,
            )

        if new_content != content:
            any_updated = True
            if not dry_run:
                makefile.write_text(new_content, encoding="utf-8")
                sys.stderr.write(f"Updated {makefile} to version {version}\n")

    return any_updated


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate derived dependency files from the runtime manifest.")
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
    updated_feeds = update_feeds(deps, dry_run=check)
    updated_gw_req = write_gateway_requirements(deps, dry_run=check)
    updated_gw_makefile = update_gateway_makefile(deps, dry_run=check)

    fail = False
    if check and (
        updated_requirements
        or updated_makefile
        or updated_pyproject
        or updated_feeds
        or updated_gw_req
        or updated_gw_makefile
    ):
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
