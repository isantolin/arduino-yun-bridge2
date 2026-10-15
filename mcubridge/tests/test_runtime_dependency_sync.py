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
    assert sync_runtime_deps.ECOSYSTEM_VERSION == expected_version
    assert sync_runtime_deps.USER_AGENT == f"McuBridge-DepsSync/{expected_version}"

