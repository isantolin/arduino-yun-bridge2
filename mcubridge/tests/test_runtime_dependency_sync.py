from pathlib import Path

import pytest
from typer.testing import CliRunner

from tools.audit import sync_runtime_deps


def test_write_requirements_dry_run_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requirements_path = tmp_path / "runtime.txt"
    monkeypatch.setattr(sync_runtime_deps, "REQUIREMENTS_PATH", requirements_path)
    deps: list[sync_runtime_deps.DepEntry] = [
        {
            "name": "sample",
            "openwrt": "python3-sample",
            "pip": "sample==1.2.3",
            "check_latest": False,
            "gateway": False,
            "edge": True,
        }
    ]

    assert sync_runtime_deps.write_requirements(deps, dry_run=True)
    assert not requirements_path.exists()
    assert sync_runtime_deps.write_requirements(deps)
    assert requirements_path.read_text(encoding="utf-8") == (
        "# Generated via tools/audit/sync_runtime_deps.py; do not edit.\nsample==1.2.3\n"
    )


def test_load_manifest_reports_malformed_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = tmp_path / "runtime.toml"
    manifest_path.write_text("invalid toml = [", encoding="utf-8")
    monkeypatch.setattr(sync_runtime_deps, "MANIFEST_PATH", manifest_path)

    with pytest.raises(sync_runtime_deps.ManifestError):
        sync_runtime_deps.load_manifest()


def test_cli_exposes_dry_run_option() -> None:
    result = CliRunner().invoke(sync_runtime_deps.cli, ["--help"])

    assert result.exit_code == 0
    assert "--dry-run" in result.stdout
    assert "--check-latest" in result.stdout


def test_nanopb_core_file_list_is_synced_to_installer() -> None:
    manifest = sync_runtime_deps.load_manifest()
    nanopb_core = next(dep for dep in manifest.cpp if dep["name"] == "nanopb_core")
    installer = sync_runtime_deps.ARDUINO_INSTALL_SCRIPT_PATH.read_text(encoding="utf-8")
    expected_array = "NANOPB_CORE_FILES=(" + " ".join(f'"{name}"' for name in nanopb_core["files"]) + ")"

    assert nanopb_core["files"]
    assert f'NANOPB_VERSION="{nanopb_core["version"]}"' in installer
    assert expected_array in installer
    assert not sync_runtime_deps.update_cpp_install_script(manifest.cpp, dry_run=True)


def test_wolfssl_archive_pin_is_synced_to_installer() -> None:
    manifest = sync_runtime_deps.load_manifest()
    wolfssl = next(dep for dep in manifest.cpp if dep["name"] == "wolfssl")
    installer = sync_runtime_deps.ARDUINO_INSTALL_SCRIPT_PATH.read_text(encoding="utf-8")

    assert wolfssl["version"] == "5.8.4"
    assert wolfssl["sha256"] == "a3199eb282afdff6ed7ee438a175842a8c425a4264022019153e430def20ef96"
    assert not wolfssl["check_latest"]
    assert f'WOLFSSL_VERSION="{wolfssl["version"]}"' in installer
    assert f'WOLFSSL_SHA256="{wolfssl["sha256"]}"' in installer
    assert "wolfssl-${WOLFSSL_VERSION}.zip" in installer
    assert not sync_runtime_deps.update_cpp_install_script(manifest.cpp, dry_run=True)


def test_check_latest_versions_skips_disabled_cpp_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = sync_runtime_deps.load_manifest()
    wolfssl = next(dep for dep in manifest.cpp if dep["name"] == "wolfssl")

    def unexpected_http_request(*args: object, **kwargs: object) -> None:
        pytest.fail(f"unexpected dependency update request: {args}, {kwargs}")

    monkeypatch.setattr(sync_runtime_deps.httpx, "get", unexpected_http_request)

    assert not sync_runtime_deps.check_latest_versions([], [wolfssl])


def test_main_dry_run_reports_up_to_date_when_no_changes() -> None:
    result = CliRunner().invoke(sync_runtime_deps.cli, ["--dry-run"])

    assert result.exit_code == 0
    assert "[dry-run] All dependency manifests are up to date." in result.stdout


def test_update_workflows_preserves_action_inputs_and_updates_literal_pins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflows_dir = tmp_path / ".github" / "workflows"
    actions_dir = tmp_path / ".github" / "actions" / "setup-protoc"
    workflows_dir.mkdir(parents=True)
    actions_dir.mkdir(parents=True)

    action_file = actions_dir / "action.yml"
    action_file.write_text(
        'PROTOC_VERSION="${{ inputs.version }}"\n',
        encoding="utf-8",
    )
    workflow_file = workflows_dir / "ci.yml"
    workflow_file.write_text(
        "PROTOC_VERSION=35.0\nprotobuf==7.35.0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sync_runtime_deps, "ROOT", tmp_path)

    deps: list[sync_runtime_deps.DepEntry] = [
        {
            "name": "protobuf",
            "openwrt": "python3-protobuf",
            "pip": "protobuf==7.36.2",
            "check_latest": True,
            "gateway": True,
            "edge": True,
        }
    ]
    assert sync_runtime_deps.update_workflows(deps)

    assert action_file.read_text(encoding="utf-8") == 'PROTOC_VERSION="${{ inputs.version }}"\n'
    assert workflow_file.read_text(encoding="utf-8") == "PROTOC_VERSION=36.2\nprotobuf==7.36.2\n"


def test_update_tox_dev_deps_syncs_pinned_versions_without_prefix_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tox_file = tmp_path / "tox.ini"
    tox_file.write_text(
        "deps =\n    protobuf==1.0.0\n    mypy-protobuf==5.0.0\n    types-protobuf==7.30.0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sync_runtime_deps, "TOX_PATH", tox_file)

    dev_deps: list[sync_runtime_deps.DevDepEntry] = [
        {"name": "mypy-protobuf", "pip": "mypy-protobuf==5.1.0", "rationale": "", "check_latest": True},
        {"name": "types-protobuf", "pip": "types-protobuf==7.35.1", "rationale": "", "check_latest": True},
    ]
    runtime_deps: list[sync_runtime_deps.DepEntry] = [
        {
            "name": "protobuf",
            "openwrt": "python3-protobuf",
            "pip": "protobuf==7.36.2",
            "check_latest": True,
            "gateway": True,
            "edge": True,
        },
    ]

    assert sync_runtime_deps.update_tox_dev_deps(dev_deps, runtime_deps)
    expected = "deps =\n    protobuf==7.36.2\n    mypy-protobuf==5.1.0\n    types-protobuf==7.35.1\n"
    assert tox_file.read_text(encoding="utf-8") == expected


def test_sync_runtime_deps_user_agent_reflects_ecosystem_version() -> None:
    expected_version = sync_runtime_deps.VERSION_PATH.read_text(encoding="utf-8").strip()
    assert expected_version == sync_runtime_deps.ECOSYSTEM_VERSION
    assert f"McuBridge-DepsSync/{expected_version}" == sync_runtime_deps.USER_AGENT


def test_sync_metadata_updates_only_version_tokens(tmp_path: Path) -> None:
    files = {
        "pyproject.toml": '[project]\nversion = "1.0.0"\ndescription = "keep"\n',
        "mcubridge/mcubridge/__init__.py": '__version__ = "1.0.0"\nOTHER = "1.0.0"\n',
        "mcubridge/Makefile": "PKG_VERSION:=1.0.0\nOTHER:=keep\n",
        "mcubridge-gateway/Makefile": "PKG_VERSION:=1.0.0\n",
        "luci-app-mcubridge/Makefile": "PKG_VERSION:=1.0.0\n",
        "mcubridge-library-arduino/library.properties": "version=1.0.0\nname=Bridge\n",
    }
    for relative_path, content in files.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    changed_dry = sync_runtime_deps.sync_metadata("2.3.4", tmp_path, dry_run=True)
    assert {path.relative_to(tmp_path).as_posix() for path in changed_dry} == set(files)
    assert 'version = "1.0.0"' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")

    changed = sync_runtime_deps.sync_metadata("2.3.4", tmp_path)
    assert {path.relative_to(tmp_path).as_posix() for path in changed} == set(files)
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == (
        '[project]\nversion = "2.3.4"\ndescription = "keep"\n'
    )
    assert (tmp_path / "mcubridge/mcubridge/__init__.py").read_text(encoding="utf-8") == (
        '__version__ = "2.3.4"\nOTHER = "1.0.0"\n'
    )

    with pytest.raises(ValueError, match="Invalid ecosystem version"):
        sync_runtime_deps.sync_metadata("not_a_pep440_version!", tmp_path)


def test_check_latest_versions_enforces_pigweed_constraints() -> None:
    pigweed_dep: sync_runtime_deps.DepEntry = {
        "name": "pigweed",
        "openwrt": "python3-pigweed",
        "pip": "pigweed==0.0.24",
        "check_latest": False,
        "gateway": True,
        "edge": True,
    }
    deps_violating: list[sync_runtime_deps.DepEntry] = [
        pigweed_dep,
        {
            "name": "protobuf",
            "openwrt": "python3-protobuf",
            "pip": "protobuf==7.36.2",
            "check_latest": True,
            "gateway": True,
            "edge": True,
        },
    ]
    outdated = sync_runtime_deps.check_latest_versions(deps_violating)
    assert any("violates authoritative Pigweed requirement" in item[0] for item in outdated)

    deps_satisfying: list[sync_runtime_deps.DepEntry] = [
        pigweed_dep,
        {
            "name": "protobuf",
            "openwrt": "python3-protobuf",
            "pip": "protobuf==5.28.3",
            "check_latest": True,
            "gateway": True,
            "edge": True,
        },
    ]
    outdated_satisfying = sync_runtime_deps.check_latest_versions(deps_satisfying)
    assert not any("violates authoritative Pigweed requirement" in item[0] for item in outdated_satisfying)


def test_nanopb_toolchain_versions_match_pigweed_compatibility_set() -> None:
    manifest = sync_runtime_deps.load_manifest()
    runtime = {dep["name"]: dep for dep in manifest.runtime}
    cpp = {dep["name"]: dep for dep in manifest.cpp}

    assert runtime["pigweed"]["pip"] == "pigweed==0.0.24"
    assert runtime["protobuf"]["pip"] == "protobuf==5.28.3"
    assert runtime["nanopb"]["pip"] == "nanopb==0.4.9.1"
    assert cpp["nanopb_core"]["version"] == "0.4.9.2"

    bootstrap_paths = (
        sync_runtime_deps.ROOT / ".github/actions/setup-python-tox/action.yml",
        sync_runtime_deps.ROOT / ".github/workflows/build-apk.yml",
        sync_runtime_deps.ROOT / ".github/workflows/ci.yml",
        sync_runtime_deps.ROOT / ".github/workflows/publish-packages.yml",
    )
    for path in bootstrap_paths:
        assert "nanopb==0.4.9.1" in path.read_text(encoding="utf-8"), str(path)
