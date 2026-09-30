from pathlib import Path

import pytest
from typer.testing import CliRunner

from tools.audit import sync_runtime_deps


def test_write_requirements_dry_run_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requirements_path = tmp_path / "runtime.txt"
    monkeypatch.setattr(sync_runtime_deps, "REQUIREMENTS_PATH", requirements_path)
    deps = [
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
    manifest_path.write_text("[[dependency]\nname = 'unterminated\n", encoding="utf-8")
    monkeypatch.setattr(sync_runtime_deps, "MANIFEST_PATH", manifest_path)

    with pytest.raises(sync_runtime_deps.ManifestError, match="Malformed manifest"):
        sync_runtime_deps.load_manifest()


def test_cli_exposes_dry_run_option() -> None:
    result = CliRunner().invoke(sync_runtime_deps.cli, ["--help"])

    assert result.exit_code == 0
    assert "--dry-run" in result.stdout
    assert "--check-latest" in result.stdout


def _mock_sync_true(*_args: object, **_kwargs: object) -> bool:
    return True


def _mock_sync_false(*_args: object, **_kwargs: object) -> bool:
    return False


def _mock_empty_manifest() -> sync_runtime_deps.ManifestData:
    return sync_runtime_deps.ManifestData([], [], [])


def test_main_dry_run_reports_preview_when_changes_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync_runtime_deps, "load_manifest", _mock_empty_manifest)
    monkeypatch.setattr(sync_runtime_deps, "write_requirements", _mock_sync_true)
    monkeypatch.setattr(sync_runtime_deps, "update_makefile", _mock_sync_false)
    monkeypatch.setattr(sync_runtime_deps, "update_pyproject", _mock_sync_false)
    monkeypatch.setattr(sync_runtime_deps, "update_feeds", _mock_sync_false)
    monkeypatch.setattr(sync_runtime_deps, "write_gateway_requirements", _mock_sync_false)
    monkeypatch.setattr(sync_runtime_deps, "update_gateway_makefile", _mock_sync_false)
    monkeypatch.setattr(sync_runtime_deps, "update_cpp_install_script", _mock_sync_false)
    monkeypatch.setattr(sync_runtime_deps, "update_tox_dev_deps", _mock_sync_false)
    monkeypatch.setattr(sync_runtime_deps, "update_workflows", _mock_sync_false)

    result = CliRunner().invoke(sync_runtime_deps.cli, ["--dry-run"])

    assert result.exit_code == 0
    assert "[dry-run] The following files/manifests would be modified:" in result.stdout
    assert str(sync_runtime_deps.REQUIREMENTS_PATH.relative_to(sync_runtime_deps.ROOT)) in result.stdout


def test_main_dry_run_reports_up_to_date_when_no_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync_runtime_deps, "load_manifest", _mock_empty_manifest)
    monkeypatch.setattr(sync_runtime_deps, "write_requirements", _mock_sync_false)
    monkeypatch.setattr(sync_runtime_deps, "update_makefile", _mock_sync_false)
    monkeypatch.setattr(sync_runtime_deps, "update_pyproject", _mock_sync_false)
    monkeypatch.setattr(sync_runtime_deps, "update_feeds", _mock_sync_false)
    monkeypatch.setattr(sync_runtime_deps, "write_gateway_requirements", _mock_sync_false)
    monkeypatch.setattr(sync_runtime_deps, "update_gateway_makefile", _mock_sync_false)
    monkeypatch.setattr(sync_runtime_deps, "update_cpp_install_script", _mock_sync_false)
    monkeypatch.setattr(sync_runtime_deps, "update_tox_dev_deps", _mock_sync_false)
    monkeypatch.setattr(sync_runtime_deps, "update_workflows", _mock_sync_false)

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

    deps = [
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
