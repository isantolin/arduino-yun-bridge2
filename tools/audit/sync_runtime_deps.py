#!/usr/bin/env python3
"""Generate derived dependency files from the runtime manifest."""

from __future__ import annotations

import re
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, TypedDict, cast

import httpx
import tenacity
import typer
from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[2]
VERSION_PATH = ROOT / "VERSION"
ECOSYSTEM_VERSION = VERSION_PATH.read_text(encoding="utf-8").strip() if VERSION_PATH.exists() else "2.8.8"
USER_AGENT = f"McuBridge-DepsSync/{ECOSYSTEM_VERSION}"
MANIFEST_PATH = ROOT / "requirements" / "runtime.toml"
REQUIREMENTS_PATH = ROOT / "requirements" / "runtime.txt"
PYPROJECT_PATH = ROOT / "pyproject.toml"
MAKEFILE_PATH = ROOT / "mcubridge" / "Makefile"
GATEWAY_REQUIREMENTS_PATH = ROOT / "mcubridge-gateway" / "requirements.txt"
GATEWAY_MAKEFILE_PATH = ROOT / "mcubridge-gateway" / "Makefile"
FEEDS_DIR = ROOT / "feeds"
TOX_PATH = ROOT / "tox.ini"
ARDUINO_INSTALL_SCRIPT_PATH = ROOT / "mcubridge-library-arduino" / "tools" / "install.sh"

VERSION_TARGETS: tuple[tuple[str, str, str, int], ...] = (
    ("pyproject.toml", r'version\s*=\s*"[^"]+"', 'version = "{version}"', 1),
    ("mcubridge/mcubridge/__init__.py", r'__version__\s*=\s*"[^"]+"', '__version__ = "{version}"', 1),
    ("mcubridge/Makefile", r"PKG_VERSION:=[^\n]+", "PKG_VERSION:={version}", 0),
    ("mcubridge-gateway/Makefile", r"PKG_VERSION:=[^\n]+", "PKG_VERSION:={version}", 0),
    ("luci-app-mcubridge/Makefile", r"PKG_VERSION:=[^\n]+", "PKG_VERSION:={version}", 0),
    ("mcubridge-library-arduino/library.properties", r"version=[^\n]+", "version={version}", 0),
)

BLOCK_START = "# AUTO-GENERATED RUNTIME DEPENDS BEGIN"
BLOCK_END = "# AUTO-GENERATED RUNTIME DEPENDS END"
CPP_BLOCK_START = "# --- [AUTO-GENERATED C++ DEPENDENCIES BEGIN] ---"
CPP_BLOCK_END = "# --- [AUTO-GENERATED C++ DEPENDENCIES END] ---"

SYSTEM_ONLY_PACKAGES = {"uci", "ubus"}
BUILD_ONLY_PACKAGES = {"jinja2", "nanopb", "xxd", "black"}


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
    files: list[str]


class DevDepEntry(TypedDict):
    name: str
    pip: str
    rationale: str
    check_latest: bool


@dataclass(slots=True, frozen=True)
class ManifestData:
    runtime: list[DepEntry]
    cpp: list[_CppDepEntry]
    dev: list[DevDepEntry]


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

    normalized_runtime = [
        DepEntry(
            name=entry.get("name") or entry.get("openwrt", "").strip() or "(unnamed)",
            openwrt=entry.get("openwrt", "").strip(),
            pip=entry.get("pip", "").strip(),
            check_latest=bool(entry.get("check_latest", True)),
            gateway=bool(entry.get("gateway", False)),
            edge=bool(entry.get("edge", True)),
        )
        for entry in entries
    ]

    normalized_cpp: list[_CppDepEntry] = []
    for entry in data.get("cpp_dependency", []):
        raw_files = entry.get("files", [])
        if not isinstance(raw_files, list):
            raise ManifestError(f"Invalid files list for C++ dependency: {entry.get('name', '(unnamed)')}")
        files: list[str] = []
        for file_name in cast(list[object], raw_files):
            if not isinstance(file_name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", file_name):
                raise ManifestError(f"Invalid files list for C++ dependency: {entry.get('name', '(unnamed)')}")
            files.append(file_name)
        normalized_cpp.append(
            _CppDepEntry(
                name=entry.get("name", "").strip(),
                github=entry.get("github", "").strip(),
                ref_type=entry.get("ref_type", "tags").strip(),
                version=entry.get("version", "").strip(),
                check_file=entry.get("check_file", "").strip(),
                rationale=entry.get("rationale", "").strip(),
                target_dir=entry.get("target_dir", "").strip(),
                files=files,
            )
        )

    normalized_dev = [
        DevDepEntry(
            name=entry.get("name", "").strip(),
            pip=entry.get("pip", "").strip(),
            rationale=entry.get("rationale", "").strip(),
            check_latest=bool(entry.get("check_latest", True)),
        )
        for entry in data.get("dev_dependency", [])
    ]
    return ManifestData(runtime=normalized_runtime, cpp=normalized_cpp, dev=normalized_dev)


def collect_pip_specs(deps: Sequence[DepEntry], *, gateway: bool = False) -> list[str]:
    specs = {
        dep["pip"]
        for dep in deps
        if dep.get("pip") and dep["name"] not in BUILD_ONLY_PACKAGES and (gateway or dep.get("edge", True))
    }
    return sorted(s for s in specs if not any(s.startswith(p) for p in SYSTEM_ONLY_PACKAGES))


def collect_openwrt_packages(deps: Sequence[DepEntry], *, edge_only: bool = False) -> list[str]:
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


def sync_metadata(
    version: str = ECOSYSTEM_VERSION,
    repo_root: Path = ROOT,
    *,
    dry_run: bool = False,
) -> list[Path]:
    """Synchronize package metadata versions from VERSION using packaging.version.Version."""
    try:
        Version(version)
    except InvalidVersion as exc:
        raise ValueError(f"Invalid ecosystem version: {version!r}") from exc

    changed: list[Path] = []
    for relative_path, pattern, replacement, count in VERSION_TARGETS:
        target = repo_root / relative_path
        if not target.is_file():
            continue
        original = target.read_text(encoding="utf-8")
        updated, matches = re.subn(pattern, replacement.format(version=version), original, count=count)
        if matches == 0:
            raise RuntimeError(f"Could not find version metadata to update in {target}")
        if _write_if_changed(target, updated, dry_run=dry_run):
            changed.append(target)
    return changed


def write_requirements(deps: Sequence[DepEntry], *, dry_run: bool = False) -> bool:
    lines = ["# Generated via tools/audit/sync_runtime_deps.py; do not edit.", *collect_pip_specs(deps)]
    return _write_if_changed(REQUIREMENTS_PATH, "\n".join(lines) + "\n", dry_run=dry_run)


def write_gateway_requirements(deps: Sequence[DepEntry], *, dry_run: bool = False) -> bool:
    gw_deps = [dep for dep in deps if dep.get("gateway")]
    specs = collect_pip_specs(gw_deps, gateway=True)
    lines = ["# Generated via tools/audit/sync_runtime_deps.py; do not edit.", *specs]
    return _write_if_changed(GATEWAY_REQUIREMENTS_PATH, "\n".join(lines) + "\n", dry_run=dry_run)


def update_pyproject(deps: Sequence[DepEntry], *, dry_run: bool = False) -> bool:
    if not PYPROJECT_PATH.exists():
        return False
    content = PYPROJECT_PATH.read_text(encoding="utf-8")
    body = "\n".join(f'    "{spec}",' for spec in collect_pip_specs(deps))
    new_content = re.sub(r"(?ms)^dependencies = \[\n.*?\n\]", f"dependencies = [\n{body}\n]", content, count=1)
    return _write_if_changed(PYPROJECT_PATH, new_content, dry_run=dry_run)


def format_openwrt_lines(tokens: Sequence[str]) -> list[str]:
    return [f"\t\t+{token}" + (" \\" if i < len(tokens) - 1 else "") for i, token in enumerate(tokens)]


def _replace_marked_block(content: str, start_marker: str, end_marker: str, rendered_block: str) -> str:
    pattern = rf"(?ms)^([ \t]*{re.escape(start_marker)}\n).*?(^[ \t]*{re.escape(end_marker)})"
    return re.sub(pattern, lambda m: f"{m.group(1)}{rendered_block}\n{m.group(2)}", content, count=1)


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
    block_lines = ["\tDEPENDS+= \\", *format_openwrt_lines(tokens)] if tokens else ["\tDEPENDS+="]
    new_text = _replace_marked_block(makefile_text, BLOCK_START, BLOCK_END, "\n".join(block_lines))
    return _write_if_changed(path, new_text, dry_run=dry_run)


def update_makefile(deps: Sequence[DepEntry], *, dry_run: bool = False) -> bool:
    return _update_makefile(MAKEFILE_PATH, deps, edge_only=True, dry_run=dry_run)


def update_gateway_makefile(deps: Sequence[DepEntry], *, dry_run: bool = False) -> bool:
    if not GATEWAY_MAKEFILE_PATH.exists():
        return False
    return _update_makefile(
        GATEWAY_MAKEFILE_PATH, [d for d in deps if d.get("gateway")], dry_run=dry_run, label="Gateway Makefile"
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
        if dep["name"] == "nanopb_core":
            if not dep["files"]:
                raise ManifestError("nanopb_core must list its runtime files")
            quoted_files = " ".join(f'"{file_name}"' for file_name in dep["files"])
            lines.append(f"NANOPB_CORE_FILES=({quoted_files})")

    new_content = _replace_marked_block(content, CPP_BLOCK_START, CPP_BLOCK_END, "\n".join(lines))
    return _write_if_changed(ARDUINO_INSTALL_SCRIPT_PATH, new_content, dry_run=dry_run)


def update_tox_dev_deps(
    dev_deps: Sequence[DevDepEntry], runtime_deps: Sequence[DepEntry] = (), *, dry_run: bool = False
) -> bool:
    """Synchronize pinned versions in tox.ini with manifest declarations."""
    if not TOX_PATH.exists():
        return False
    content = TOX_PATH.read_text(encoding="utf-8")
    new_content = content
    for spec in [d["pip"] for d in (*dev_deps, *runtime_deps) if d.get("pip")]:
        name, version = _parse_pip_spec(spec)
        if name and version:
            new_content = re.sub(rf"(?m)^([ \t]*){re.escape(name)}==[^\s\n]+", rf"\g<1>{name}=={version}", new_content)
    return _write_if_changed(TOX_PATH, new_content, dry_run=dry_run)


def _parse_pip_spec(spec: str) -> tuple[str, str]:
    """Extract (package_name, pinned_version) from a pip spec using packaging.Requirement."""
    if not spec:
        return "", ""
    try:
        req = Requirement(spec)
        pinned = next((s.version for s in req.specifier if s.operator in ("==", "===")), "")
        return req.name, pinned
    except (InvalidRequirement, ValueError):
        if "==" not in spec:
            return spec, ""
        name_part, version = spec.split("==", 1)
        return name_part.split("[")[0].strip(), version.strip()


def fetch_url_with_retry(
    url: str, headers: dict[str, str] | None = None, timeout: float = 10.0, attempts: int = 3
) -> httpx.Response:
    """Fetch URL contents with exponential backoff using tenacity and HTTPX."""
    retryer = tenacity.Retrying(
        stop=tenacity.stop_after_attempt(attempts),
        wait=tenacity.wait_exponential(multiplier=0.5, min=0.5, max=4.0),
        retry=tenacity.retry_if_exception_type((httpx.HTTPError, TimeoutError, OSError)),
        reraise=True,
    )
    return retryer(httpx.get, url, headers=headers, timeout=timeout)


def _fetch_latest_version(package_name: str, *, include_prerelease: bool = False) -> str | None:
    """Query PyPI JSON API for the latest release version using packaging.Version."""
    try:
        resp = fetch_url_with_retry(f"https://pypi.org/pypi/{package_name}/json", headers={"User-Agent": USER_AGENT})
        data = resp.json()
        if data.get("releases"):
            parsed: list[Version] = []
            for v_str in data["releases"]:
                try:
                    v = Version(v_str)
                    if include_prerelease or not v.is_prerelease:
                        parsed.append(v)
                except (InvalidVersion, ValueError) as exc:
                    sys.stderr.write(f"[DEBUG] Skipping unparseable version string '{v_str}': {exc}\n")
            if parsed:
                return str(max(parsed))
        return str(data["info"]["version"])
    except (httpx.HTTPError, ValueError, KeyError, tenacity.RetryError) as exc:
        sys.stderr.write(f"[WARN] Failed fetching latest version for {package_name}: {exc}\n")
        return None


def _fetch_pypi_sdist_hash(package_name: str, version: str) -> str | None:
    """Fetch SHA-256 hash of the sdist package from PyPI JSON API."""
    try:
        resp = fetch_url_with_retry(
            f"https://pypi.org/pypi/{package_name}/{version}/json", headers={"User-Agent": USER_AGENT}
        )
        for file_info in resp.json().get("urls", []):
            if file_info.get("packagetype") == "sdist":
                return str(file_info.get("digests", {}).get("sha256") or "")
        return None
    except (httpx.HTTPError, ValueError, KeyError, tenacity.RetryError) as exc:
        sys.stderr.write(f"[WARN] Failed fetching sdist hash for {package_name}=={version}: {exc}\n")
        return None


def _fetch_github_latest_version(repo: str) -> str | None:
    """Query GitHub API for latest release or tag using HTTPX."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github.v3+json"}
    try:
        latest_url = f"https://api.github.com/repos/{repo}/releases/latest"
        resp = fetch_url_with_retry(latest_url, headers=headers, timeout=8.0)
        tag: Any = cast(dict[str, Any], resp.json()).get("tag_name")
        if tag is not None:
            return str(tag)
    except (httpx.HTTPError, ValueError, KeyError, IndexError, tenacity.RetryError) as exc:
        sys.stderr.write(f"[DEBUG] GitHub latest release failed for {repo} ({exc}), falling back to tags...\n")
        try:
            tag_resp = fetch_url_with_retry(f"https://api.github.com/repos/{repo}/tags", headers=headers, timeout=8.0)
            tags_data = cast(list[dict[str, Any]], tag_resp.json())
            if tags_data and "name" in tags_data[0]:
                return str(tags_data[0]["name"])
        except (httpx.HTTPError, ValueError, KeyError, IndexError, tenacity.RetryError) as tag_exc:
            sys.stderr.write(f"[WARN] GitHub tags query failed for {repo}: {tag_exc}\n")
    return None


def _is_version_newer(pinned: str, latest: str) -> bool:
    try:
        return Version(latest) > Version(pinned)
    except (InvalidVersion, TypeError):
        return latest != pinned


def check_latest_versions(
    deps: Sequence[DepEntry],
    cpp_deps: Sequence[_CppDepEntry] = (),
    dev_deps: Sequence[DevDepEntry] = (),
) -> list[tuple[str, str, str]]:
    """Return list of (package, pinned, latest) for outdated packages."""
    outdated: list[tuple[str, str, str]] = []
    for entry in (*deps, *dev_deps):
        if not entry.get("check_latest", True) or not entry.get("pip"):
            continue
        name, pinned = _parse_pip_spec(entry["pip"])
        if not pinned:
            continue
        try:
            is_pre = Version(pinned).is_prerelease
        except (InvalidVersion, TypeError):
            is_pre = any(tag in pinned for tag in ("rc", "a", "b", "dev"))
        latest_str = _fetch_latest_version(name, include_prerelease=is_pre)
        if latest_str and _is_version_newer(pinned, latest_str):
            outdated.append((name, pinned, latest_str))

    for cpp_dep in cpp_deps:
        if cpp_dep["ref_type"] == "heads":
            continue
        pinned = cpp_dep["version"]
        gh_latest = _fetch_github_latest_version(cpp_dep["github"])
        if gh_latest and _is_version_newer(
            pinned.lstrip("v").removeprefix("nanopb-"), gh_latest.lstrip("v").removeprefix("nanopb-")
        ):
            outdated.append((cpp_dep["name"], pinned, gh_latest))
    return outdated


def _to_apk_version(version: str) -> str:
    """Convert Python pre-release notation to APK (Alpine) version notation."""
    try:
        if not Version(version).is_prerelease:
            return version
    except (InvalidVersion, TypeError) as exc:
        sys.stderr.write(f"[DEBUG] Failed to parse version string '{version}': {exc}\n")
    converted = re.sub(r"(\d)a(\d+)$", r"\1_alpha\2", version)
    converted = re.sub(r"(\d)b(\d+)$", r"\1_beta\2", converted)
    converted = re.sub(r"(\d)rc(\d+)$", r"\1_rc\2", converted)
    return re.sub(r"\.dev(\d+)$", r"_pre\1", converted)


def update_feeds(deps: Sequence[DepEntry], *, dry_run: bool = False, verify_hashes: bool = False) -> bool:
    if not FEEDS_DIR.exists():
        return False

    any_updated = False
    for dep in deps:
        openwrt_pkg = dep.get("openwrt", "")
        pip_name, version = _parse_pip_spec(dep.get("pip", ""))
        makefile = FEEDS_DIR / openwrt_pkg / "Makefile"
        if not openwrt_pkg.startswith("python3-") or not version or not makefile.exists():
            continue

        content = makefile.read_text(encoding="utf-8")
        pkg_version = _to_apk_version(version)
        new_content = re.sub(r"PKG_VERSION:=[^\n]+", f"PKG_VERSION:={pkg_version}", content)
        if re.search(r"^\s*include\b.*\bpypi\.mk\b", content, re.MULTILINE):
            for obsolete in (
                "PYTHON3_PKG_WHEEL_VERSION",
                "PKG_SOURCE",
                "PKG_BUILD_DIR",
                "PYPI_SOURCE_NAME",
                "PYPI_SOURCE_NAME_VERSION",
            ):
                new_content = re.sub(rf"{obsolete}:=[^\n]+\n?", "", new_content)
        else:
            pkg_src = f"PKG_SOURCE:={pip_name}-{version}.tar.gz"
            new_content = re.sub(r"PKG_SOURCE:=[^\n]+\.tar\.gz", pkg_src, new_content)
            new_content = re.sub(
                r"PKG_BUILD_DIR:=[^\n]+", f"PKG_BUILD_DIR:=$(BUILD_DIR)/pypi/{pip_name}-{version}", new_content
            )

        version_changed = new_content != content
        if "PKG_HASH:=" in content and (
            verify_hashes or version_changed or bool(re.search(r"PKG_HASH:=\s*$", content, re.MULTILINE))
        ):
            new_hash = _fetch_pypi_sdist_hash(pip_name, version)
            if new_hash:
                new_content = re.sub(r"PKG_HASH:=[^\n]+", f"PKG_HASH:={new_hash}", new_content)

        if _write_if_changed(makefile, new_content, dry_run=dry_run):
            any_updated = True
            if not dry_run:
                sys.stderr.write(f"Updated {makefile} to version {version}\n")
    return any_updated


def update_workflows(deps: Sequence[DepEntry], *, dry_run: bool = False) -> bool:
    workflows_dir = ROOT / ".github" / "workflows"
    actions_dir = ROOT / ".github" / "actions"
    if not workflows_dir.exists():
        return False

    pb_dep = next((d for d in deps if d.get("name") == "protobuf"), None)
    _, protobuf_pip_version = _parse_pip_spec(pb_dep.get("pip", "") if pb_dep else "")
    if not protobuf_pip_version:
        return False
    parts = protobuf_pip_version.split(".")
    is_modern_pb = len(parts) >= 3 and parts[0] in {"7", "6", "5", "4"}
    protoc_version = f"{parts[1]}.{parts[2]}" if is_modern_pb else protobuf_pip_version

    target_files = sorted(workflows_dir.glob("*.yml"))
    if actions_dir.exists():
        target_files.extend(sorted(actions_dir.glob("**/*.yml")))

    any_updated = False
    for wf in target_files:
        content = wf.read_text(encoding="utf-8")
        new_content = re.sub(r"""PROTOC_VERSION=(?!["']?\$\{\{)[^\n]+""", f"PROTOC_VERSION={protoc_version}", content)
        new_content = re.sub(r"protobuf==\d+\.\d+(\.\d+)?", f"protobuf=={protobuf_pip_version}", new_content)
        if _write_if_changed(wf, new_content, dry_run=dry_run):
            any_updated = True
            if not dry_run:
                sys.stderr.write(f"Updated {wf.name} (protoc={protoc_version}, protobuf={protobuf_pip_version})\n")
    return any_updated


cli = typer.Typer(help="Generate derived dependency files from the runtime manifest.", add_completion=False)


@cli.command()
def main(
    check: Annotated[bool, typer.Option("--check", help="Exit with status 1 if files would change")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show whether files would change without writing")] = False,
    check_latest: Annotated[bool, typer.Option("--check-latest", help="Check PyPI/GitHub for outdated pins")] = False,
    print_openwrt: Annotated[bool, typer.Option("--print-openwrt", help="Print OpenWrt package names")] = False,
    print_pip: Annotated[bool, typer.Option("--print-pip", help="Print pip requirement specifiers")] = False,
) -> None:
    manifest = load_manifest()
    deps, cpp_deps, dev_deps = manifest.runtime, manifest.cpp, manifest.dev

    if print_openwrt:
        sys.stdout.write("\n".join(collect_openwrt_packages(deps)) + "\n")
        raise SystemExit(0)
    if print_pip:
        sys.stdout.write("\n".join(collect_pip_specs(deps)) + "\n")
        raise SystemExit(0)

    no_write = check or dry_run
    version_str = VERSION_PATH.read_text(encoding="utf-8").strip() if VERSION_PATH.is_file() else ECOSYSTEM_VERSION
    changed_paths = [str(p.relative_to(ROOT)) for p in sync_metadata(version_str, ROOT, dry_run=no_write)]
    updates = (
        (write_requirements(deps, dry_run=no_write), REQUIREMENTS_PATH),
        (update_makefile(deps, dry_run=no_write), MAKEFILE_PATH),
        (update_pyproject(deps, dry_run=no_write), PYPROJECT_PATH),
        (update_feeds(deps, dry_run=no_write, verify_hashes=check_latest or not no_write), FEEDS_DIR),
        (write_gateway_requirements(deps, dry_run=no_write), GATEWAY_REQUIREMENTS_PATH),
        (update_gateway_makefile(deps, dry_run=no_write), GATEWAY_MAKEFILE_PATH),
        (update_cpp_install_script(cpp_deps, dry_run=no_write), ARDUINO_INSTALL_SCRIPT_PATH),
        (update_tox_dev_deps(dev_deps, deps, dry_run=no_write), TOX_PATH),
        (update_workflows(deps, dry_run=no_write), ROOT / ".github" / "workflows"),
    )
    for updated, path in updates:
        rel_str = str(path.relative_to(ROOT))
        if updated and rel_str not in changed_paths:
            changed_paths.append(rel_str)

    fail = bool(check and changed_paths)
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
