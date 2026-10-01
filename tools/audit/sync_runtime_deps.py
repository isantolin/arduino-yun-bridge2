#!/usr/bin/env python3
"""Generate derived dependency files from the runtime manifest."""

from __future__ import annotations

import json
import re
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, TypedDict, cast

import tenacity
import typer
from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "requirements" / "runtime.toml"
REQUIREMENTS_PATH = ROOT / "requirements" / "runtime.txt"
PYPROJECT_PATH = ROOT / "pyproject.toml"
MAKEFILE_PATH = ROOT / "mcubridge" / "Makefile"
GATEWAY_REQUIREMENTS_PATH = ROOT / "mcubridge-gateway" / "requirements.txt"
GATEWAY_MAKEFILE_PATH = ROOT / "mcubridge-gateway" / "Makefile"
FEEDS_DIR = ROOT / "feeds"
TOX_PATH = ROOT / "tox.ini"
ARDUINO_INSTALL_SCRIPT_PATH = ROOT / "mcubridge-library-arduino" / "tools" / "install.sh"

BLOCK_START = "# AUTO-GENERATED RUNTIME DEPENDS BEGIN"
BLOCK_END = "# AUTO-GENERATED RUNTIME DEPENDS END"
CPP_BLOCK_START = "# --- [AUTO-GENERATED C++ DEPENDENCIES BEGIN] ---"
CPP_BLOCK_END = "# --- [AUTO-GENERATED C++ DEPENDENCIES END] ---"

# --- [FILTRADO INTELIGENTE DE DEPENDENCIAS] ---

# uci / ubus: Solo en OpenWrt (Makefile), no en pip (runtime.txt) para evitar errores locales.
SYSTEM_ONLY_PACKAGES = {"uci", "ubus"}

# Dev/CI host build-only packages (excluded from OpenWrt MPU package dependencies).
BUILD_ONLY_PACKAGES = {"jinja2", "nanopb", "grpcio-tools", "xxd", "black"}


class ManifestError(RuntimeError):
    """Raised when the manifest file is missing or malformed."""


class DepEntry(TypedDict):
    name: str
    openwrt: str
    pip: str
    check_latest: bool
    gateway: bool
    edge: bool


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
    runtime: list[DepEntry]
    cpp: list[_CppDepEntry]
    dev: list[_DevDepEntry]


def load_manifest() -> ManifestData:
    if not MANIFEST_PATH.exists():
        raise ManifestError(f"Missing manifest: {MANIFEST_PATH}")

    try:
        with MANIFEST_PATH.open("rb") as manifest_file:
            data = tomllib.load(manifest_file)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"Malformed manifest: {MANIFEST_PATH}: {exc}") from exc
    entries = data.get("dependency")
    if not entries:
        raise ManifestError("Manifest must declare at least one dependency")
    normalized_runtime: list[DepEntry] = []
    for entry in entries:
        openwrt = entry.get("openwrt", "").strip()
        pip_spec = entry.get("pip", "").strip()
        name = entry.get("name") or openwrt or "(unnamed)"
        normalized_runtime.append(
            DepEntry(
                name=name,
                openwrt=openwrt,
                pip=pip_spec,
                check_latest=bool(entry.get("check_latest", True)),
                gateway=bool(entry.get("gateway", False)),
                edge=bool(entry.get("edge", True)),
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


def collect_pip_specs(deps: Sequence[DepEntry]) -> list[str]:
    # Mantiene todo EXCEPTO los paquetes exclusivos de sistema (uci)
    specs = {dep["pip"] for dep in deps if dep.get("pip")}
    filtered = {s for s in specs if not any(s.startswith(p) for p in SYSTEM_ONLY_PACKAGES)}
    return sorted(filtered)


def collect_openwrt_packages(deps: Sequence[DepEntry], *, edge_only: bool = False) -> list[str]:
    # Mantiene todo EXCEPTO los paquetes exclusivos de construcción (jinja2, etc)
    # Esto asegura que el APK sea ultra-lean.
    return [
        dep["openwrt"]
        for dep in deps
        if dep.get("openwrt") and dep["name"] not in BUILD_ONLY_PACKAGES and (not edge_only or dep.get("edge", True))
    ]


def _write_if_changed(path: Path, content: str, *, dry_run: bool = False) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    if not dry_run:
        path.write_text(content, encoding="utf-8")
    return True


def write_requirements(deps: Sequence[DepEntry], *, dry_run: bool = False) -> bool:
    pip_specs = collect_pip_specs(deps)
    content = ["# Generated via tools/audit/sync_runtime_deps.py; do not edit."]
    content.extend(pip_specs)
    return _write_if_changed(REQUIREMENTS_PATH, "\n".join(content) + "\n", dry_run=dry_run)


def write_gateway_requirements(deps: Sequence[DepEntry], *, dry_run: bool = False) -> bool:
    gateway_deps = [dep for dep in deps if dep.get("gateway")]
    pip_specs = collect_pip_specs(gateway_deps)
    content = ["# Generated via tools/audit/sync_runtime_deps.py; do not edit."]
    content.extend(pip_specs)
    return _write_if_changed(GATEWAY_REQUIREMENTS_PATH, "\n".join(content) + "\n", dry_run=dry_run)


def update_pyproject(deps: Sequence[DepEntry], *, dry_run: bool = False) -> bool:
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

    return _write_if_changed(PYPROJECT_PATH, new_content, dry_run=dry_run)


def format_openwrt_lines(tokens: Sequence[str]) -> list[str]:
    lines: list[str] = []
    for index, token in enumerate(tokens):
        suffix = " \\" if index < len(tokens) - 1 else ""
        lines.append(f"\t\t{token}{suffix}")
    return lines


def _update_makefile(
    path: Path,
    deps: Sequence[DepEntry],
    *,
    edge_only: bool = False,
    dry_run: bool = False,
    label: str = "Makefile",
) -> bool:
    makefile_text = path.read_text(encoding="utf-8")
    if BLOCK_START not in makefile_text or BLOCK_END not in makefile_text:
        raise ManifestError(f"{label} is missing dependency markers; cannot inject dependencies")
    tokens = collect_openwrt_packages(deps, edge_only=edge_only)
    if tokens:
        block_lines = ["\tDEPENDS+= \\"]
        block_lines.extend(format_openwrt_lines(tokens))
    else:
        block_lines = ["\tDEPENDS+="]
    rendered_block = "\n".join(block_lines)
    new_lines: list[str] = []
    in_block = False
    for line in makefile_text.splitlines():
        if BLOCK_START in line:
            in_block = True
            new_lines.extend((line, rendered_block))
            continue
        if BLOCK_END in line:
            in_block = False
            new_lines.append(line)
            continue
        if not in_block:
            new_lines.append(line)
    return _write_if_changed(path, "\n".join(new_lines) + "\n", dry_run=dry_run)


def update_makefile(deps: Sequence[DepEntry], *, dry_run: bool = False) -> bool:
    return _update_makefile(MAKEFILE_PATH, deps, edge_only=True, dry_run=dry_run)


def update_gateway_makefile(deps: Sequence[DepEntry], *, dry_run: bool = False) -> bool:
    if not GATEWAY_MAKEFILE_PATH.exists():
        return False
    gateway_deps = [dep for dep in deps if dep.get("gateway")]
    return _update_makefile(
        GATEWAY_MAKEFILE_PATH,
        gateway_deps,
        dry_run=dry_run,
        label="Gateway Makefile",
    )


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
    except (InvalidRequirement, ValueError):
        if "==" not in spec:
            return spec, ""
        name_part, version = spec.split("==", 1)
        name = name_part.split("[")[0].strip()
        return name, version.strip()


def fetch_url_with_retry(
    req: urllib.request.Request,
    timeout: float = 10.0,
    attempts: int = 3,
) -> bytes:
    """Fetch URL contents with exponential backoff using tenacity."""
    retryer = tenacity.Retrying(
        stop=tenacity.stop_after_attempt(attempts),
        wait=tenacity.wait_exponential(multiplier=0.5, min=0.5, max=4.0),
        retry=tenacity.retry_if_exception_type((urllib.error.URLError, TimeoutError, OSError)),
        reraise=True,
    )

    def _call() -> bytes:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    return retryer(_call)


def _fetch_latest_version(package_name: str, *, include_prerelease: bool = False) -> str | None:
    """Query PyPI JSON API for the latest release version using packaging.Version."""
    url = f"https://pypi.org/pypi/{package_name}/json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "McuBridge-DepsSync/2.8"})
        data = json.loads(fetch_url_with_retry(req, timeout=10.0).decode("utf-8"))
        if "releases" in data and data["releases"]:
            parsed_versions: list[Version] = []
            for v_str in data["releases"].keys():
                try:
                    v = Version(v_str)
                    if not include_prerelease and v.is_prerelease:
                        continue
                    parsed_versions.append(v)
                except (InvalidVersion, ValueError) as exc:
                    sys.stderr.write(f"[DEBUG] Skipping unparseable version string '{v_str}': {exc}\n")
                    continue
            if parsed_versions:
                parsed_versions.sort()
                return str(parsed_versions[-1])
        return str(data["info"]["version"])
    except (urllib.error.URLError, ValueError, KeyError, json.JSONDecodeError, tenacity.RetryError) as exc:
        sys.stderr.write(f"[WARN] Failed fetching latest version for {package_name}: {exc}\n")
        return None


def _fetch_pypi_sdist_hash(package_name: str, version: str) -> str | None:
    """Fetch SHA-256 hash of the sdist package from PyPI JSON API."""
    url = f"https://pypi.org/pypi/{package_name}/{version}/json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "McuBridge-DepsSync/2.8"})
        data = json.loads(fetch_url_with_retry(req, timeout=10.0).decode("utf-8"))
        for file_info in data.get("urls", []):
            if file_info.get("packagetype") == "sdist":
                return str(file_info.get("digests", {}).get("sha256") or "")
        return None
    except (urllib.error.URLError, ValueError, KeyError, json.JSONDecodeError, tenacity.RetryError) as exc:
        sys.stderr.write(f"[WARN] Failed fetching sdist hash for {package_name}=={version}: {exc}\n")
        return None


def _fetch_github_latest_version(repo: str) -> str | None:
    """Query GitHub API for latest release or tag using standard library."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "McuBridge-DepsSync/2.8", "Accept": "application/vnd.github.v3+json"},
    )
    try:
        data = cast(dict[str, Any], json.loads(fetch_url_with_retry(req, timeout=8.0).decode("utf-8")))
        tag: Any = data.get("tag_name")
        if tag is not None:
            return str(tag)
    except (
        urllib.error.URLError,
        json.JSONDecodeError,
        KeyError,
        IndexError,
        UnicodeDecodeError,
        tenacity.RetryError,
        TimeoutError,
        OSError,
    ) as exc:
        sys.stderr.write(f"[DEBUG] GitHub latest release failed for {repo} ({exc}), falling back to tags...\n")
        tag_url = f"https://api.github.com/repos/{repo}/tags"
        tag_req = urllib.request.Request(
            tag_url,
            headers={"User-Agent": "McuBridge-DepsSync/2.8", "Accept": "application/vnd.github.v3+json"},
        )
        try:
            tags_data = cast(
                list[dict[str, Any]],
                json.loads(fetch_url_with_retry(tag_req, timeout=8.0).decode("utf-8")),
            )
            if tags_data and "name" in tags_data[0]:
                return str(tags_data[0]["name"])
        except (
            urllib.error.URLError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            UnicodeDecodeError,
            tenacity.RetryError,
            TimeoutError,
            OSError,
        ) as tag_exc:
            sys.stderr.write(f"[WARN] GitHub tags query failed for {repo}: {tag_exc}\n")
            return None
    return None


def _check_runtime_outdated(deps: Sequence[DepEntry]) -> list[tuple[str, str, str]]:
    outdated: list[tuple[str, str, str]] = []
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
        except (InvalidVersion, TypeError):
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
            except (InvalidVersion, TypeError):
                if latest_str != pinned:
                    outdated.append((name, pinned, latest_str))
    return outdated


def _check_dev_outdated(dev_deps: Sequence[_DevDepEntry]) -> list[tuple[str, str, str]]:
    outdated: list[tuple[str, str, str]] = []
    for dev_dep in dev_deps:
        name, pinned = _parse_pip_spec(dev_dep["pip"])
        if not pinned:
            continue
        latest_str = _fetch_latest_version(name)
        if latest_str:
            try:
                if Version(latest_str) > Version(pinned):
                    outdated.append((name, pinned, latest_str))
            except (InvalidVersion, TypeError):
                if latest_str != pinned:
                    outdated.append((name, pinned, latest_str))
    return outdated


def _check_cpp_outdated(cpp_deps: Sequence[_CppDepEntry]) -> list[tuple[str, str, str]]:
    outdated: list[tuple[str, str, str]] = []
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
            except (InvalidVersion, TypeError):
                if gh_latest != pinned:
                    outdated.append((cpp_dep["name"], pinned, gh_latest))
    return outdated


def check_latest_versions(
    deps: Sequence[DepEntry],
    cpp_deps: Sequence[_CppDepEntry] = (),
    dev_deps: Sequence[_DevDepEntry] = (),
) -> list[tuple[str, str, str]]:
    """Return list of (package, pinned, latest) for outdated packages using packaging.Version & GitHub API."""
    outdated = _check_runtime_outdated(deps)
    outdated.extend(_check_dev_outdated(dev_deps))
    outdated.extend(_check_cpp_outdated(cpp_deps))
    return outdated


def _to_apk_version(version: str) -> str:
    """Convert Python pre-release notation to APK (Alpine) version notation."""
    is_prerelease = True
    try:
        is_prerelease = Version(version).is_prerelease
    except (InvalidVersion, TypeError) as exc:
        sys.stderr.write(f"[DEBUG] Failed to parse version string '{version}': {exc}\n")
        is_prerelease = True

    if not is_prerelease:
        return version

    converted = re.sub(r"(\d)a(\d+)$", r"\1_alpha\2", version)
    converted = re.sub(r"(\d)b(\d+)$", r"\1_beta\2", converted)
    converted = re.sub(r"(\d)rc(\d+)$", r"\1_rc\2", converted)
    return re.sub(r"\.dev(\d+)$", r"_pre\1", converted)


def update_feeds(deps: Sequence[DepEntry], *, dry_run: bool = False) -> bool:
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

        if "PKG_HASH:=" in content:
            new_hash = _fetch_pypi_sdist_hash(pip_name, version)
            if new_hash:
                new_content = re.sub(r"PKG_HASH:=[^\n]+", f"PKG_HASH:={new_hash}", new_content)

        if new_content != content:
            any_updated = True
            if not dry_run:
                makefile.write_text(new_content, encoding="utf-8")
                sys.stderr.write(f"Updated {makefile} to version {version}\n")

    return any_updated


def update_workflows(deps: Sequence[DepEntry], *, dry_run: bool = False) -> bool:
    workflows_dir = ROOT / ".github" / "workflows"
    actions_dir = ROOT / ".github" / "actions"
    if not workflows_dir.exists():
        return False

    protoc_version = ""
    protobuf_pip_version = ""
    for dep in deps:
        if dep.get("name") == "protobuf":
            _, p_ver = _parse_pip_spec(dep.get("pip", ""))
            if p_ver:
                protobuf_pip_version = p_ver
                parts = p_ver.split(".")
                if len(parts) >= 3 and parts[0] in {"7", "6", "5", "4"}:
                    protoc_version = f"{parts[1]}.{parts[2]}"
                else:
                    protoc_version = p_ver
                break

    if not protoc_version:
        return False

    target_files: list[Path] = sorted(workflows_dir.glob("*.yml"))
    if actions_dir.exists():
        target_files.extend(sorted(actions_dir.glob("**/*.yml")))

    any_updated = False
    for wf in target_files:
        content = wf.read_text(encoding="utf-8")
        new_content = re.sub(
            r"PROTOC_VERSION=(?![\"']?\$\{\{)[^\n]+",
            f"PROTOC_VERSION={protoc_version}",
            content,
        )
        if protobuf_pip_version:
            new_content = re.sub(
                r"protobuf==\d+\.\d+(\.\d+)?",
                f"protobuf=={protobuf_pip_version}",
                new_content,
            )
        if new_content != content:
            any_updated = True
            if not dry_run:
                wf.write_text(new_content, encoding="utf-8")
                sys.stderr.write(f"Updated {wf.name} (protoc={protoc_version}, protobuf={protobuf_pip_version})\n")

    return any_updated


cli = typer.Typer(help="Generate derived dependency files from the runtime manifest.", add_completion=False)


@cli.command()
def main(
    check: Annotated[
        bool,
        typer.Option("--check", help="Exit with status 1 if running would change any files"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show whether files would change without writing them"),
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

    no_write = check or dry_run
    updated_requirements = write_requirements(deps, dry_run=no_write)
    updated_makefile = update_makefile(deps, dry_run=no_write)
    updated_pyproject = update_pyproject(deps, dry_run=no_write)
    updated_feeds = update_feeds(deps, dry_run=no_write)
    updated_gw_req = write_gateway_requirements(deps, dry_run=no_write)
    updated_gw_makefile = update_gateway_makefile(deps, dry_run=no_write)
    updated_cpp = update_cpp_install_script(cpp_deps, dry_run=no_write)
    updated_tox = update_tox_dev_deps(dev_deps, dry_run=no_write)
    updated_workflows = update_workflows(deps, dry_run=no_write)

    changed_paths: list[str] = []
    for updated, path in (
        (updated_requirements, REQUIREMENTS_PATH),
        (updated_makefile, MAKEFILE_PATH),
        (updated_pyproject, PYPROJECT_PATH),
        (updated_feeds, FEEDS_DIR),
        (updated_gw_req, GATEWAY_REQUIREMENTS_PATH),
        (updated_gw_makefile, GATEWAY_MAKEFILE_PATH),
        (updated_cpp, ARDUINO_INSTALL_SCRIPT_PATH),
        (updated_tox, TOX_PATH),
        (updated_workflows, ROOT / ".github" / "workflows"),
    ):
        if updated:
            changed_paths.append(str(path.relative_to(ROOT)))

    fail = False
    if check and changed_paths:
        fail = True

    if dry_run:
        if changed_paths:
            print("[dry-run] The following files/manifests would be modified:")
            for path in changed_paths:
                print(f"  {path}")
        else:
            print("[dry-run] All dependency manifests are up to date.")

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
