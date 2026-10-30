"""Tests for cross-platform agent configuration auditing and generation."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, cast

from pytest_mock import MockerFixture
from typer.testing import CliRunner

from tools.audit.check_gemini_parity import (
    AGENT_JSON,
    CANONICAL_SKILLS_DIR,
    COPILOT_SCOPED_INSTRUCTIONS,
    COPILOT_SKILLS_DIR,
    CHATGPT_SKILL_EXPORT_SCRIPT,
    GENERATED_AGENT_PROFILES,
    GEMINI_WORKFLOW_ONLY_SKILLS,
    COPILOT_AGENT_PROFILE,
    MODEL_FILE,
    REQUIRED_SHARED_SKILLS,
    SYMLINKS,
    app,
    check_chatgpt_skill_export,
    check_copilot_agent_profile,
    check_github_action_model_wiring,
    check_model_parity,
    check_prompt_parity,
    check_rules_parity,
    check_shared_skill_parity,
    check_gemini_workflow_skill_scoping,
    sync_generated_files,
)

runner = CliRunner()


def _write_minimal_agent_fixture(root: Path) -> str:
    canonical = "\n".join(
        [
            "---",
            "name: test-agent",
            'description: "Test agent profile"',
            "---",
            "",
            "# Canonical Rules",
            "",
        ]
    )
    (root / "GEMINI.md").write_text(canonical, encoding="utf-8")
    for relative_path, target in SYMLINKS.items():
        link = root / relative_path
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    (root / ".agents").symlink_to(".agent")
    for relative_path in GENERATED_AGENT_PROFILES:
        profile = root / relative_path
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text(canonical, encoding="utf-8")
    copilot_profile = root / COPILOT_AGENT_PROFILE
    copilot_profile.parent.mkdir(parents=True, exist_ok=True)
    copilot_body = canonical.split("---", 2)[2].lstrip("\r\n").strip()
    copilot_frontmatter = (
        "---\n"
        "name: openwrt-architect-arduino\n"
        "description: Test architect\n"
        "tools: [read, edit, search, execute]\n"
        "---\n\n"
    )
    copilot_profile.write_text(
        copilot_frontmatter + copilot_body + "\n",
        encoding="utf-8",
    )
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
    copilot_instructions.write_text(
        "Canonical Skills: .agents/skills/ and .github/skills/ mirror\n",
        encoding="utf-8",
    )
    source_skill_root = root / CANONICAL_SKILLS_DIR
    mirror_skill_root = root / COPILOT_SKILLS_DIR
    for workflow_skill_name in GEMINI_WORKFLOW_ONLY_SKILLS:
        workflow_skill = source_skill_root / workflow_skill_name / "SKILL.md"
        workflow_skill.parent.mkdir(parents=True, exist_ok=True)
        workflow_skill.write_text(
            "---\n"
            f"name: {workflow_skill_name}\n"
            "description: Workflow-only prompt for Gemini GitHub Actions.\n"
            "---\n\n"
            "## Workflow-only scope\n"
            "Do not activate it for normal interactive repository engineering.\n",
            encoding="utf-8",
        )
    for skill_name in REQUIRED_SHARED_SKILLS:
        skill = source_skill_root / skill_name / "SKILL.md"
        mirror = mirror_skill_root / skill_name / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        mirror.parent.mkdir(parents=True, exist_ok=True)
        content = f"---\nname: {skill_name}\ndescription: A test skill for validation.\n---\n\n# Test skill\n"
        skill.write_text(content, encoding="utf-8")
        mirror.write_text(content, encoding="utf-8")
    exporter = root / CHATGPT_SKILL_EXPORT_SCRIPT
    exporter.parent.mkdir(parents=True, exist_ok=True)
    exporter.write_text("\n".join(f'"{name}"' for name in REQUIRED_SHARED_SKILLS), encoding="utf-8")
    for relative_path in COPILOT_SCOPED_INSTRUCTIONS:
        instruction = root / relative_path
        instruction.parent.mkdir(parents=True, exist_ok=True)
        instruction.write_text("---\napplyTo: \"**/*.py\"\n---\n\nTest scope.\n", encoding="utf-8")
    return canonical


def test_check_model_parity_success() -> None:
    assert check_model_parity() == []



def test_check_chatgpt_skill_export_requires_every_shared_skill(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    _write_minimal_agent_fixture(tmp_path)
    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)
    assert check_chatgpt_skill_export() == []
    exporter = tmp_path / CHATGPT_SKILL_EXPORT_SCRIPT
    exporter.write_text('"ci-failure-diagnosis"\n', encoding="utf-8")
    errors = check_chatgpt_skill_export()
    assert any("does not package shared Skills" in error for error in errors)


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


def test_check_github_action_model_wiring_requires_canonical_file(mocker: MockerFixture, tmp_path: Path) -> None:
    (tmp_path / ".agent" / "workflows").mkdir(parents=True)
    (tmp_path / ".agent" / "workflows" / "gemini-example.md").write_text(
        "---\ndescription: Example\n---\n\nPrompt\n",
        encoding="utf-8",
    )
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "gemini-example.yml").write_text(
        "gemini_model: gemini-4-argon\n",
        encoding="utf-8",
    )
    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)
    errors = check_github_action_model_wiring()
    assert len(errors) == 1
    assert "tools/gemini_model" in errors[0]


def test_gemini_workflow_skills_are_explicitly_scoped(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    _write_minimal_agent_fixture(tmp_path)
    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)
    assert check_gemini_workflow_skill_scoping() == []
    skill_file = tmp_path / CANONICAL_SKILLS_DIR / GEMINI_WORKFLOW_ONLY_SKILLS[0] / "SKILL.md"
    skill_file.write_text(
        "---\n"
        "name: gemini-invoke\n"
        "description: General repository Skill.\n"
        "---\n\n"
        "# Generic\n",
        encoding="utf-8",
    )
    errors = check_gemini_workflow_skill_scoping()
    assert any("workflow-only prompt" in error for error in errors)
    assert any("prohibit general interactive activation" in error for error in errors)


def test_check_rules_parity_requires_symlinks_and_generated_profiles(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    _write_minimal_agent_fixture(tmp_path)
    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)
    assert check_rules_parity() == []
    assert check_copilot_agent_profile() == []
    (tmp_path / "AGENTS.md").unlink()
    (tmp_path / "AGENTS.md").write_text("copied rules\n", encoding="utf-8")
    errors = check_rules_parity()
    assert any("AGENTS.md must be a symlink" in error for error in errors)


def test_check_rules_parity_requires_agents_directory_symlink(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    _write_minimal_agent_fixture(tmp_path)
    agent_link = tmp_path / ".agents"
    agent_link.unlink()
    agent_link.mkdir()
    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)
    errors = check_rules_parity()
    assert any(".agents must remain a Git symlink to .agent" in error for error in errors)


def test_check_rules_parity_rejects_obsolete_copilot_duplicate(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    _write_minimal_agent_fixture(tmp_path)
    (tmp_path / ".github" / "copilot-instructions").write_text("duplicate\n", encoding="utf-8")
    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)
    assert any("Obsolete duplicate exists" in error for error in check_rules_parity())


def test_check_shared_skill_parity_rejects_stale_mirror(mocker: MockerFixture, tmp_path: Path) -> None:
    _write_minimal_agent_fixture(tmp_path)
    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)
    mirror_skill = tmp_path / COPILOT_SKILLS_DIR / REQUIRED_SHARED_SKILLS[0] / "SKILL.md"
    mirror_skill.write_text("stale skill\n", encoding="utf-8")
    errors = check_shared_skill_parity()
    assert any("mirror is stale" in error for error in errors)


def test_check_shared_skill_parity_requires_valid_skill_metadata(mocker: MockerFixture, tmp_path: Path) -> None:
    _write_minimal_agent_fixture(tmp_path)
    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)
    source_skill = tmp_path / CANONICAL_SKILLS_DIR / REQUIRED_SHARED_SKILLS[0] / "SKILL.md"
    source_skill.write_text(
        "---\nname: wrong-name\ndescription: Valid description\n---\n\nBody\n",
        encoding="utf-8",
    )
    errors = check_shared_skill_parity()
    assert any("frontmatter name must match" in error for error in errors)


def test_sync_generated_files_preserves_agent_metadata_and_generates_outputs(
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
    copilot_profile = (tmp_path / COPILOT_AGENT_PROFILE).read_text(encoding="utf-8")
    assert "tools: [read, edit, search, execute]" in copilot_profile
    assert copilot_profile.endswith("# Canonical Rules\n")
    assert check_copilot_agent_profile() == []
    for skill_name in REQUIRED_SHARED_SKILLS:
        source_skill = tmp_path / CANONICAL_SKILLS_DIR / skill_name / "SKILL.md"
        mirror_skill = tmp_path / COPILOT_SKILLS_DIR / skill_name / "SKILL.md"
        assert source_skill.read_text(encoding="utf-8") == mirror_skill.read_text(encoding="utf-8")
    gemini_only_skill = tmp_path / ".agent" / "skills" / "gemini-invoke" / "SKILL.md"
    assert gemini_only_skill.is_file()
    assert not (tmp_path / COPILOT_SKILLS_DIR / "gemini-invoke").exists()
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
    assert check_shared_skill_parity() == []


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
        "\n".join(
            [
                "      - name: 'Checkout repository'",
                "        uses: 'actions/checkout@v7'",
                "      - name: 'Read canonical Gemini model'",
                "        id: 'gemini_model'",
                "        run: |-",
                "          model=''",
                "          IFS= read -r model < tools/gemini_model || true",
                "      - uses: 'google-github-actions/run-gemini-cli@v0'",
                "        with:",
                "          gemini_model: '${{ steps.gemini_model.outputs.model }}'",
            ]
        ),
        encoding="utf-8",
    )
    mocker.patch("tools.audit.check_gemini_parity.ROOT", tmp_path)
    first = runner.invoke(cast(Any, app), ["--fix"])
    second = runner.invoke(cast(Any, app), [])
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "All cross-platform agent parity checks passed" in second.stdout
