"""Tests for the Gemini agent configuration auditor and generator."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, cast

from pytest_mock import MockerFixture
from typer.testing import CliRunner

from tools.audit.check_gemini_parity import (
    AGENT_JSON,
    GENERATED_AGENT_PROFILES,
    MODEL_FILE,
    SYMLINKS,
    app,
    check_environment_model_parity,
    check_github_action_model_wiring,
    check_model_parity,
    check_prompt_parity,
    check_rules_parity,
    sync_generated_files,
)

runner = CliRunner()


def _write_minimal_agent_fixture(root: Path) -> str:
    canonical = (
        "---\n"
        "name: test-agent\n"
        'description: "Test agent profile"\n'
        "---\n\n"
        "# Canonical Rules\n"
    )
    (root / "GEMINI.md").write_text(canonical, encoding="utf-8")
    for relative_path, target in SYMLINKS.items():
        link = root / relative_path
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    for relative_path in GENERATED_AGENT_PROFILES:
        profile = root / relative_path
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text(canonical, encoding="utf-8")
    agent_path = root / AGENT_JSON
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    (root / MODEL_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / MODEL_FILE).write_text("test-model\n", encoding="utf-8")
    agent_path.write_text(
        json.dumps({"name": "test-agent", "model": "test-model", "instructions": canonical}),
        encoding="utf-8",
    )
    copilot_instructions = root / ".github" / "copilot-instructions.md"
    copilot_instructions.parent.mkdir(parents=True, exist_ok=True)
    copilot_instructions.write_text("# Copilot-only guidance\n", encoding="utf-8")
    return canonical


def test_check_model_parity_success() -> None:
    assert check_model_parity() == []


def test_check_environment_model_parity_matches(mocker: MockerFixture, tmp_path: Path) -> None:
    (tmp_path / MODEL_FILE).parent.mkdir(parents=True)
    (tmp_path / MODEL_FILE).write_text("test-model\n", encoding="utf-8")
    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)
    mocker.patch.dict("os.environ", {"GEMINI_MODEL": "test-model"})
    assert check_environment_model_parity() == []


def test_check_environment_model_parity_requires_configured_model(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    (tmp_path / MODEL_FILE).parent.mkdir(parents=True)
    (tmp_path / MODEL_FILE).write_text("test-model\n", encoding="utf-8")
    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)
    mocker.patch.dict("os.environ", {}, clear=True)
    assert "GEMINI_MODEL is unset" in check_environment_model_parity()[0]


def test_check_environment_model_parity_detects_drift(mocker: MockerFixture, tmp_path: Path) -> None:
    (tmp_path / MODEL_FILE).parent.mkdir(parents=True)
    (tmp_path / MODEL_FILE).write_text("canonical-model\n", encoding="utf-8")
    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)
    mocker.patch.dict("os.environ", {"GEMINI_MODEL": "different-model"})
    assert "!= canonical model" in check_environment_model_parity()[0]


def test_check_prompt_parity_detects_mismatched_command(mocker: MockerFixture, tmp_path: Path) -> None:
    workflow_dir = tmp_path / ".agent" / "workflows"
    command_dir = tmp_path / ".github" / "commands"
    workflow_dir.mkdir(parents=True)
    command_dir.mkdir(parents=True)
    (workflow_dir / "gemini-example.md").write_text(
        "---\ndescription: Example command\n---\n\nCanonical prompt\n",
        encoding="utf-8",
    )
    (command_dir / "gemini-example.toml").write_text(
        'description = "Example command"\nprompt = "Outdated prompt"\n',
        encoding="utf-8",
    )
    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)
    errors = check_prompt_parity()
    assert len(errors) == 1
    assert "prompt mismatch" in errors[0]


def test_check_github_action_model_wiring_requires_canonical_variable(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    workflow_dir = tmp_path / ".agent" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "gemini-example.md").write_text(
        "---\ndescription: Example\n---\n\nPrompt\n",
        encoding="utf-8",
    )
    action_dir = tmp_path / ".github" / "workflows"
    action_dir.mkdir(parents=True)
    (action_dir / "gemini-example.yml").write_text(
        "with:\n  gemini_model: gemini-4-argon\n",
        encoding="utf-8",
    )
    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)
    errors = check_github_action_model_wiring()
    assert len(errors) == 1
    assert "vars.GEMINI_MODEL" in errors[0]


def test_check_rules_parity_requires_symlinks_and_generated_profiles(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    _write_minimal_agent_fixture(tmp_path)
    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)
    assert check_rules_parity() == []
    (tmp_path / "AGENTS.md").unlink()
    (tmp_path / "AGENTS.md").write_text("copied rules\n", encoding="utf-8")
    errors = check_rules_parity()
    assert any("AGENTS.md must be a symlink" in error for error in errors)


def test_check_rules_parity_rejects_obsolete_copilot_duplicate(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    _write_minimal_agent_fixture(tmp_path)
    (tmp_path / ".github" / "copilot-instructions").write_text("duplicate\n", encoding="utf-8")
    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)
    assert any("Obsolete duplicate exists" in error for error in check_rules_parity())


def test_sync_generated_files_preserves_agent_metadata_and_generates_toml(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    canonical = _write_minimal_agent_fixture(tmp_path)
    workflow_dir = tmp_path / ".agent" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "gemini-example.md").write_text(
        "---\ndescription: Example command\n---\n\nPrompt body\n",
        encoding="utf-8",
    )
    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)
    assert sync_generated_files() == []
    for relative_path in GENERATED_AGENT_PROFILES:
        assert (tmp_path / relative_path).read_text(encoding="utf-8") == canonical
    agent = json.loads((tmp_path / AGENT_JSON).read_text(encoding="utf-8"))
    assert agent["name"] == "test-agent"
    assert agent["model"] == "test-model"
    assert agent["instructions"] == canonical
    command = tomllib.loads(
        (tmp_path / ".github" / "commands" / "gemini-example.toml").read_text(encoding="utf-8")
    )
    assert command == {"description": "Example command", "prompt": "Prompt body"}
    assert check_model_parity() == []
    assert check_prompt_parity() == []


def test_cli_fix_is_repeatable_and_checks_generated_artifacts(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    _write_minimal_agent_fixture(tmp_path)
    workflow_dir = tmp_path / ".agent" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "gemini-example.md").write_text(
        "---\ndescription: Example command\n---\n\nPrompt body\n",
        encoding="utf-8",
    )
    action_dir = tmp_path / ".github" / "workflows"
    action_dir.mkdir(parents=True)
    (action_dir / "gemini-example.yml").write_text(
        "with:\\n  gemini_model: vars.GEMINI_MODEL\\n",
        encoding="utf-8",
    )
    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)
    first = runner.invoke(cast(Any, app), ["--fix"])
    second = runner.invoke(cast(Any, app), [])
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "All agent parity checks passed" in second.stdout
