"""Deterministic packaging, CLI contract, and hardening tests for mcubridge-gateway-cpp."""

from __future__ import annotations

import configparser
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_DIR = REPO_ROOT / "mcubridge-gateway-cpp"
GATEWAY_MAIN = GATEWAY_DIR / "src" / "gateway_main.cc"
CMAKELISTS = GATEWAY_DIR / "CMakeLists.txt"
DOCKERFILE = GATEWAY_DIR / "Dockerfile"
DOCKERIGNORE = GATEWAY_DIR / "Dockerfile.dockerignore"
SYSTEMD_UNIT = GATEWAY_DIR / "deploy" / "mcubridge-gateway.service"
ENV_EXAMPLE = GATEWAY_DIR / "deploy" / "mcubridge-gateway.env.example"
BUILD_APK_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-apk.yml"

BINARY = Path(
    os.environ.get(
        "MCUBRIDGE_GATEWAY_BINARY",
        REPO_ROOT / "build/mcubridge-gateway/mcubridge-gateway-cpp",
    )
)


def _extract_cli_flags_from_source() -> set[str]:
    source = GATEWAY_MAIN.read_text(encoding="utf-8")
    return set(re.findall(r'Argument\(argc,\s*argv,\s*"(--[a-z-]+)"', source))


def _extract_default_port_from_source() -> str:
    source = GATEWAY_MAIN.read_text(encoding="utf-8")
    match = re.search(r'Argument\(argc,\s*argv,\s*"--port",\s*"(\d+)"\)', source)
    assert match is not None, "Could not locate default --port in gateway_main.cc"
    return match.group(1)


def _extract_pinned_versions() -> dict[str, str]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps: list[str] = pyproject["project"]["dependencies"]
    pinned: dict[str, str] = {}
    for dep in deps:
        if "==" in dep:
            name, version = dep.split("==", 1)
            pinned[name.strip()] = version.strip()
    return pinned


def test_cmake_defines_gnu_install_rules() -> None:
    content = CMAKELISTS.read_text(encoding="utf-8")
    assert "include(GNUInstallDirs)" in content
    assert "install(TARGETS mcubridge-gateway-cpp" in content
    assert "${CMAKE_INSTALL_BINDIR}" in content
    assert "deploy/mcubridge-gateway.service" in content
    assert "deploy/mcubridge-gateway.env.example" in content
    assert "${CMAKE_INSTALL_DATADIR}/mcubridge-gateway" in content


def test_dockerfile_is_hardened_multistage_and_matches_cli() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")
    cli_flags = _extract_cli_flags_from_source()
    default_port = _extract_default_port_from_source()
    pinned = _extract_pinned_versions()

    assert re.search(r"^FROM\s+\S+\s+AS\s+builder$", content, re.MULTILINE)
    assert re.search(r"^FROM\s+\S+\s+AS\s+runtime$", content, re.MULTILINE)
    assert f'"pigweed=={pinned["pigweed"]}"' in content
    assert f'"protobuf=={pinned["protobuf"]}"' in content
    assert "cmake --install" in content
    assert "USER mcubridge:mcubridge" in content
    assert f"EXPOSE {default_port}/tcp" in content
    assert "STOPSIGNAL SIGTERM" in content
    assert 'ENTRYPOINT ["/usr/local/bin/mcubridge-gateway-cpp"]' in content
    assert "HEALTHCHECK " in content

    for flag in cli_flags:
        assert f'"{flag}"' in content, f"Dockerfile CMD missing CLI flag {flag}"


def test_dockerfile_dockerignore_scopes_context_without_affecting_apk_feed() -> None:
    content = DOCKERIGNORE.read_text(encoding="utf-8")
    lines = [line.strip() for line in content.splitlines() if line.strip() and not line.strip().startswith("#")]
    assert lines[0] == "*"
    assert "!mcubridge-gateway-cpp/**" in lines or "!mcubridge-gateway-cpp/src/**" in lines
    assert "!tools/protocol/**" in lines
    assert "!third_party/pigweed/**" in lines

    # OpenWrt APK workflow uses context "." with Dockerfile.apk-feed copying bin/.
    apk_workflow = BUILD_APK_WORKFLOW.read_text(encoding="utf-8")
    assert "Dockerfile.apk-feed" in apk_workflow
    assert "COPY bin/ /usr/share/nginx/html/" in apk_workflow
    assert "mcubridge-gateway-cpp" not in apk_workflow


def test_systemd_unit_is_valid_hardened_and_matches_env_and_cli() -> None:
    raw = SYSTEMD_UNIT.read_text(encoding="utf-8")
    # Collapse backslash-continued lines for configparser and ExecStart inspection.
    unfolded = re.sub(r"\\\n\s*", " ", raw)

    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string(unfolded)
    assert set(parser.sections()) == {"Unit", "Service", "Install"}

    service = parser["Service"]
    assert service["Type"] == "simple"
    assert service["User"] == "mcubridge"
    assert service["Group"] == "mcubridge"
    assert service["Restart"] == "on-failure"
    assert service["KillSignal"] == "SIGTERM"
    assert service["EnvironmentFile"] == "-/etc/mcubridge-gateway/env"
    assert service["StateDirectory"] == "mcubridge-gateway"
    assert service["StateDirectoryMode"] == "0700"
    assert service["NoNewPrivileges"] == "yes"
    assert service["ProtectSystem"] == "strict"
    assert service["ProtectHome"] == "yes"
    assert service["PrivateTmp"] == "yes"
    assert service["CapabilityBoundingSet"] == ""
    assert service["AmbientCapabilities"] == ""
    assert service["RestrictAddressFamilies"] == "AF_INET AF_INET6"
    assert service["ReadOnlyPaths"] == "/etc/mcubridge-gateway"
    assert service["MemoryDenyWriteExecute"] == "yes"

    exec_start = service["ExecStart"]
    assert exec_start.startswith("/usr/local/bin/mcubridge-gateway-cpp ")
    cli_flags = _extract_cli_flags_from_source()
    for flag in cli_flags:
        assert f"{flag} " in exec_start, f"systemd ExecStart missing CLI flag {flag}"

    # Every referenced environment variable must be quoted in ExecStart, defaulted in
    # [Service], and documented in mcubridge-gateway.env.example.
    referenced_vars = set(re.findall(r'"\$\{(MCUBRIDGE_GATEWAY_[A-Z0-9_]+)\}"', exec_start))
    unquoted_vars = set(re.findall(r"(?<!\")\$\{(MCUBRIDGE_GATEWAY_[A-Z0-9_]+)\}", exec_start))
    assert not unquoted_vars, f"Unquoted variables in ExecStart risk flag-shift on empty value: {unquoted_vars}"
    assert len(referenced_vars) == len(cli_flags)

    env_defaults = set(re.findall(r"^Environment=(MCUBRIDGE_GATEWAY_[A-Z0-9_]+)=", raw, re.MULTILINE))
    assert env_defaults == referenced_vars

    env_example_text = ENV_EXAMPLE.read_text(encoding="utf-8")
    env_example_vars = set(re.findall(r"^(MCUBRIDGE_GATEWAY_[A-Z0-9_]+)=", env_example_text, re.MULTILINE))
    assert env_example_vars == referenced_vars
    default_port = _extract_default_port_from_source()
    assert f"MCUBRIDGE_GATEWAY_PORT={default_port}" in env_example_text
    assert f"Environment=MCUBRIDGE_GATEWAY_PORT={default_port}" in raw


def test_installed_binary_cli_help_and_usage_contract(tmp_path: Path) -> None:
    if not BINARY.is_file():
        pytest.skip(f"Gateway binary not found: {BINARY}")

    help_result = subprocess.run(
        [str(BINARY), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert help_result.returncode == 0
    assert help_result.stdout.startswith("usage: mcubridge-gateway-cpp ")
    for flag in _extract_cli_flags_from_source():
        assert flag in help_result.stdout

    missing_args = subprocess.run(
        [str(BINARY)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert missing_args.returncode == 2
    assert "usage: mcubridge-gateway-cpp " in missing_args.stderr

    systemd_analyze = shutil.which("systemd-analyze")
    if systemd_analyze is not None:
        staged_bin = tmp_path / "usr" / "local" / "bin" / "mcubridge-gateway-cpp"
        staged_bin.parent.mkdir(parents=True)
        shutil.copy2(BINARY, staged_bin)
        staged_unit = tmp_path / "etc" / "systemd" / "system" / "mcubridge-gateway.service"
        staged_unit.parent.mkdir(parents=True)
        shutil.copy2(SYSTEMD_UNIT, staged_unit)
        # Provide minimal implicit base targets/slices inside the isolated --root so
        # systemd-analyze verify validates our unit across both Ubuntu and Fedora systemd.
        sys_units = tmp_path / "usr" / "lib" / "systemd" / "system"
        sys_units.mkdir(parents=True)
        for target in (
            "sysinit.target",
            "basic.target",
            "shutdown.target",
            "network-online.target",
            "multi-user.target",
            "slices.target",
        ):
            (sys_units / target).write_text("[Unit]\nDescription=Test stub\n", encoding="utf-8")
        for slice_name in ("system.slice", "-.slice"):
            (sys_units / slice_name).write_text("[Unit]\nDescription=Test stub\n[Slice]\n", encoding="utf-8")
        verify = subprocess.run(
            [
                systemd_analyze,
                "verify",
                "--man=no",
                "--recursive-errors=no",
                f"--root={tmp_path}",
                "mcubridge-gateway.service",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert verify.returncode == 0, f"systemd-analyze verify failed:\n{verify.stdout}\n{verify.stderr}"
        assert verify.stderr.strip() == "", f"systemd-analyze verify reported warnings:\n{verify.stderr}"
