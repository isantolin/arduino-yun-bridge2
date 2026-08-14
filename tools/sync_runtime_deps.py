#!/usr/bin/env python3
"""Generate derived dependency files from the runtime manifest."""

from dataclasses import dataclass
import json
import re
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, TypedDict, cast

from distlib.version import NormalizedVersion
from packaging.requirements import Requirement
from packaging.version import Version
import typer

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "requirements" / "runtime.toml"
REQUIREMENTS_PATH = ROOT / "requirements" / "runtime.txt"
PYPROJECT_PATH = ROOT / "pyproject.toml"
MAKEFILE_PATH = ROOT / "mcubridge" / "Makefile"
GATEWAY_REQUIREMENTS_PATH = ROOT / "mcubridge-gateway" / "requirements.txt"
GATEWAY_MAKEFILE_PATH = ROOT / "mcubridge-gateway" / "Makefile"
FEEDS_DIR = ROOT / "feeds"
TOX_PATH = ROOT / "tox.ini"
ARDUINO_INSTALL_SCRIPT_PATH = ROOT / "mcubridge-library-arduino" / "tools" / "install.sh"
ARDUINO_LIBRARY_PROPERTIES_PATH = ROOT / "mcubridge-library-arduino" / "library.properties"

BLOCK_START = "# AUTO-GENERATED RUNTIME DEPENDS BEGIN"
BLOCK_END = "# AUTO-GENERATED RUNTIME DEPENDS END"
CPP_BLOCK_START = "# --- [AUTO-GENERATED C++ DEPENDENCIES BEGIN] ---"
CPP_BLOCK_END = "# --- [AUTO-GENERATED C++ DEPENDENCIES END] ---"

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


class _CppDepEntry(TypedDict):
    name: str
    github: str
    ref_type: str
    version: str
    check_file: str
    rationale: str
    target_dir: str


class _DevDepEntry(TypedDict):
    name: str
    pip: str
    rationale: str


@dataclass(slots=True, frozen=True)
class ManifestData:
    runtime: list[_DepEntry]
    cpp: list[_CppDepEntry]
    dev: list[_DevDepEntry]


def load_manifest() -> ManifestData:
    if not MANIFEST_PATH.exists():
        raise ManifestError(f"Missing manifest: {MANIFEST_PATH}")

    data = tomllib.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    entries = data.get("dependency")
    if not entries:
        raise ManifestError("Manifest must declare at least one dependency")
    normalized_runtime: list[_DepEntry] = []
    for entry in entries:
        openwrt = entry.get("openwrt", "").strip()
        pip_spec = entry.get("pip", "").strip()
        name = entry.get("name") or openwrt or "(unnamed)"
        normalized_runtime.append(
            _DepEntry(
                name=name,
                openwrt=openwrt,
                pip=pip_spec,
                check_latest=bool(entry.get("check_latest", True)),
                gateway=bool(entry.get("gateway", False)),
            )
        )

    normalized_cpp: list[_CppDepEntry] = []
    for entry in data.get("cpp_dependency", []):
        normalized_cpp.append(
            _CppDepEntry(
                name=entry.get("name", "").strip(),
                github=entry.get("github", "").strip(),
                ref_type=entry.get("ref_type", "tags").strip(),
                version=entry.get("version", "").strip(),
                check_file=entry.get("check_file", "").strip(),
                rationale=entry.get("rationale", "").strip(),
                target_dir=entry.get("target_dir", "").strip(),
            )
        )

    normalized_dev: list[_DevDepEntry] = []
    for entry in data.get("dev_dependency", []):
        normalized_dev.append(
            _DevDepEntry(
                name=entry.get("name", "").strip(),
                pip=entry.get("pip", "").strip(),
                rationale=entry.get("rationale", "").strip(),
            )
        )

    return ManifestData(runtime=normalized_runtime, cpp=normalized_cpp, dev=normalized_dev)


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

    # Collect only runtime dependencies for project.dependencies
    runtime_pip_specs = sorted(
        [
            dep["pip"]
            for dep in deps
            if (dep.get("pip") and not any(dep["pip"].startswith(p) for p in SYSTEM_ONLY_PACKAGES))
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
        raise ManifestError("Makefile is missing dependency markers; cannot inject dependencies")
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
        GATEWAY_MAKEFILE_PATH.write_text(updated, encoding="utf-8")
    return True


def update_cpp_install_script(cpp_deps: Sequence[_CppDepEntry], *, dry_run: bool = False) -> bool:
    """Synchronize C++ dependency versions into Arduino install.sh script."""
    if not ARDUINO_INSTALL_SCRIPT_PATH.exists():
        return False
    content = ARDUINO_INSTALL_SCRIPT_PATH.read_text(encoding="utf-8")
    if CPP_BLOCK_START not in content or CPP_BLOCK_END not in content:
        return False

    var_map = {
        "Embedded_Template_Library": "ETL_VERSION",
        "wolfSSL": "WOLFSSL_VERSION",
        "PacketSerial": "PACKETSERIAL_REF",
        "Unity": "UNITY_VERSION",
        "nanopb_core": "NANOPB_VERSION",
    }
    lines: list[str] = []
    for dep in cpp_deps:
        var_name = var_map.get(dep["name"])
        if not var_name:
            continue
        val = f"{dep['ref_type']}/{dep['version']}" if dep["ref_type"] == "heads" else dep["version"]
        lines.append(f'{var_name}="{val}"')

    rendered_block = "\n".join(lines)
    parts_before = content.split(CPP_BLOCK_START)
    parts_after = parts_before[1].split(CPP_BLOCK_END)
    new_content = f"{parts_before[0]}{CPP_BLOCK_START}\n{rendered_block}\n{CPP_BLOCK_END}{parts_after[1]}"

    if new_content == content:
        return False
    if not dry_run:
        ARDUINO_INSTALL_SCRIPT_PATH.write_text(new_content, encoding="utf-8")
    return True


def update_tox_dev_deps(dev_deps: Sequence[_DevDepEntry], *, dry_run: bool = False) -> bool:
    """Synchronize pinned versions in tox.ini with manifest dev_dependency declarations."""
    if not TOX_PATH.exists():
        return False
    content = TOX_PATH.read_text(encoding="utf-8")
    new_content = content
    for dep in dev_deps:
        name, version = _parse_pip_spec(dep["pip"])
        if name and version:
            new_content = re.sub(rf"\b{re.escape(name)}==[^\s\n]+", f"{name}=={version}", new_content)

    if new_content == content:
        return False
    if not dry_run:
        TOX_PATH.write_text(new_content, encoding="utf-8")
    return True


def _parse_pip_spec(spec: str) -> tuple[str, str]:
    """Extract (package_name, pinned_version) from a pip spec using packaging.Requirement."""
    if not spec:
        return "", ""
    try:
        req = Requirement(spec)
        name = req.name
        pinned = ""
        for specifier in req.specifier:
            if specifier.operator in ("==", "==="):
                pinned = specifier.version
                break
        return name, pinned
    except Exception:
        if "==" not in spec:
            return spec, ""
        name_part, version = spec.split("==", 1)
        name = name_part.split("[")[0].strip()
        return name, version.strip()


def _fetch_latest_version(package_name: str, *, include_prerelease: bool = False) -> str | None:
    """Query PyPI JSON API for the latest release version using packaging.Version & distlib."""
    url = f"https://pypi.org/pypi/{package_name}/json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "McuBridge-DepsSync/2.8"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "releases" in data and data["releases"]:
                parsed_versions: list[Version] = []
                for v_str in data["releases"].keys():
                    try:
                        v = Version(v_str)
                        _ = NormalizedVersion(v_str)
                        if not include_prerelease and v.is_prerelease:
                            continue
                        parsed_versions.append(v)
                    except Exception:
                        continue
                if parsed_versions:
                    parsed_versions.sort()
                    return str(parsed_versions[-1])
            return str(data["info"]["version"])
    except (urllib.error.URLError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _fetch_github_latest_version(repo: str) -> str | None:
    """Query GitHub API for latest release or tag using standard library."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "McuBridge-DepsSync/2.8", "Accept": "application/vnd.github.v3+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = cast(dict[str, Any], json.loads(resp.read().decode("utf-8")))
            tag: Any = data.get("tag_name")
            if tag is not None:
                return str(tag)
    except Exception:
        tag_url = f"https://api.github.com/repos/{repo}/tags"
        tag_req = urllib.request.Request(
            tag_url,
            headers={"User-Agent": "McuBridge-DepsSync/2.8", "Accept": "application/vnd.github.v3+json"},
        )
        try:
            with urllib.request.urlopen(tag_req, timeout=8) as resp:
                tags_data = cast(list[dict[str, Any]], json.loads(resp.read().decode("utf-8")))
                if tags_data:
                    first = tags_data[0]
                    if "name" in first:
                        return str(first["name"])
        except Exception:
            return None
    return None


def check_latest_versions(
    deps: Sequence[_DepEntry],
    cpp_deps: Sequence[_CppDepEntry] = (),
    dev_deps: Sequence[_DevDepEntry] = (),
) -> list[tuple[str, str, str]]:
    """Return list of (package, pinned, latest) for outdated packages using packaging.Version & GitHub API."""
    outdated: list[tuple[str, str, str]] = []

    # 1. Python runtime dependencies (PyPI)
    pip_specs = [(dep["pip"], dep["check_latest"]) for dep in deps if dep.get("pip")]
    for spec, should_check_latest in pip_specs:
        if not should_check_latest:
            continue
        name, pinned = _parse_pip_spec(spec)
        if not pinned:
            continue
        try:
            pinned_ver = Version(pinned)
            is_prerelease = pinned_ver.is_prerelease
        except Exception:
            is_prerelease = any(tag in pinned for tag in ("rc", "a", "b", "dev"))
            pinned_ver = None

        latest_str = _fetch_latest_version(name, include_prerelease=is_prerelease)
        if latest_str:
            try:
                latest_ver = Version(latest_str)
                if pinned_ver and latest_ver > pinned_ver:
                    outdated.append((name, pinned, latest_str))
                elif not pinned_ver and latest_str != pinned:
                    outdated.append((name, pinned, latest_str))
            except Exception:
                if latest_str != pinned:
                    outdated.append((name, pinned, latest_str))

    # 2. Development & Quality tools (PyPI)
    for dev_dep in dev_deps:
        name, pinned = _parse_pip_spec(dev_dep["pip"])
        if not pinned:
            continue
        latest_str = _fetch_latest_version(name)
        if latest_str:
            try:
                if Version(latest_str) > Version(pinned):
                    outdated.append((name, pinned, latest_str))
            except Exception:
                if latest_str != pinned:
                    outdated.append((name, pinned, latest_str))

    # 3. C++ / MCU Arduino libraries (GitHub Releases / Tags)
    for cpp_dep in cpp_deps:
        if cpp_dep["ref_type"] == "heads":
            continue
        pinned = cpp_dep["version"]
        gh_latest = _fetch_github_latest_version(cpp_dep["github"])
        if gh_latest:
            clean_pinned = pinned.lstrip("v").removeprefix("nanopb-")
            clean_latest = gh_latest.lstrip("v").removeprefix("nanopb-")
            try:
                if Version(clean_latest) > Version(clean_pinned):
                    outdated.append((cpp_dep["name"], pinned, gh_latest))
            except Exception:
                if gh_latest != pinned:
                    outdated.append((cpp_dep["name"], pinned, gh_latest))

    return outdated


def _to_apk_version(version: str) -> str:
    """Convert Python pre-release notation to APK (Alpine) version notation."""
    try:
        parsed = Version(version)
        if not parsed.is_prerelease:
            return version
    except Exception:
        pass
    version = re.sub(r"(\d)a(\d+)$", r"\1_alpha\2", version)
    version = re.sub(r"(\d)b(\d+)$", r"\1_beta\2", version)
    version = re.sub(r"(\d)rc(\d+)$", r"\1_rc\2", version)
    version = re.sub(r"\.dev(\d+)$", r"_pre\1", version)
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

        uses_pypi_mk = bool(re.search(r"^\s*include\b.*\bpypi\.mk\b", content, re.MULTILINE))
        is_prerelease = _to_apk_version(version) != version

        if uses_pypi_mk and not is_prerelease:
            pkg_version = version
            new_content = re.sub(r"PKG_VERSION:=[^\n]+", f"PKG_VERSION:={pkg_version}", content)
            new_content = re.sub(r"PYTHON3_PKG_WHEEL_VERSION:=[^\n]+\n?", "", new_content)
            new_content = re.sub(r"PKG_SOURCE:=[^\n]+\n?", "", new_content)
            new_content = re.sub(r"PKG_BUILD_DIR:=[^\n]+\n?", "", new_content)
            new_content = re.sub(r"PYPI_SOURCE_NAME:=[^\n]+\n?", "", new_content)
            new_content = re.sub(r"PYPI_SOURCE_NAME_VERSION:=[^\n]+\n?", "", new_content)
        elif uses_pypi_mk and is_prerelease:
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
            build_dir_line = f"PKG_BUILD_DIR:=$(BUILD_DIR)/pypi/{pypi_source_name}\n"
            pypi_mk_include = re.compile(r"(^\s*include\b.*\bpypi\.mk\b[^\n]*\n)", re.MULTILINE)
            if "PKG_BUILD_DIR:=" in new_content:
                new_content = re.sub(r"[ \t]*PKG_BUILD_DIR:=[^\n]+\n", "", new_content)
            new_content = pypi_mk_include.sub(lambda m: m.group(1) + build_dir_line, new_content, count=1)
        else:
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


cli = typer.Typer(help="Generate derived dependency files from the runtime manifest.", add_completion=False)


@cli.command()
def main(
    check: Annotated[
        bool,
        typer.Option("--check", help="Exit with status 1 if running would change any files"),
    ] = False,
    check_latest: Annotated[
        bool,
        typer.Option("--check-latest", help="Query PyPI and GitHub to check for outdated pinned versions"),
    ] = False,
    print_openwrt: Annotated[
        bool,
        typer.Option("--print-openwrt", help="Print OpenWrt package names and exit"),
    ] = False,
    print_pip: Annotated[
        bool,
        typer.Option("--print-pip", help="Print pip requirement specifiers and exit"),
    ] = False,
) -> None:
    manifest = load_manifest()
    deps = manifest.runtime
    cpp_deps = manifest.cpp
    dev_deps = manifest.dev

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
    updated_cpp = update_cpp_install_script(cpp_deps, dry_run=check)
    updated_tox = update_tox_dev_deps(dev_deps, dry_run=check)

    fail = False
    if check and (
        updated_requirements
        or updated_makefile
        or updated_pyproject
        or updated_feeds
        or updated_gw_req
        or updated_gw_makefile
        or updated_cpp
        or updated_tox
    ):
        fail = True

    if check_latest:
        outdated = check_latest_versions(deps, cpp_deps, dev_deps)
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
    cli()
