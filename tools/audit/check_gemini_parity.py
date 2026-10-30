#!/usr/bin/env python3
"""Validate and synchronize the repository's Gemini agent configuration.

GEMINI.md is the canonical source for repository-wide architectural instructions.
Tool-specific agent profiles and GitHub command TOML files are generated artifacts.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Annotated, cast

import typer
import yaml

ROOT = Path(__file__).resolve().parents[2]
SYMLINKS = {
    "AGENTS.md": "GEMINI.md",
    ".copilot-instructions": "GEMINI.md",
    ".agent/rules/openwrt-architect-arduino.agent.md": "../../GEMINI.md",
}
GENERATED_AGENT_PROFILES = (
    Path(".agent/agents/openwrt-architect-arduino/agent.md"),
    Path(".github/agents/openwrt-architect-arduino.agent.md"),
)
AGENT_JSON = Path(".agent/agents/openwrt-architect-arduino/agent.json")
MODEL_FILE = Path("tools/gemini_model")
OBSOLETE_COPILOT_FILE = Path(".github/copilot-instructions")


def _parse_md(path: Path) -> tuple[str, str]:
    """Return frontmatter description and prompt body from a workflow Markdown file."""
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---\n"):
        raise ValueError(f"{path}: missing YAML frontmatter")
    parts = content.split("---", 2)
    if len(parts) != 3:
        raise ValueError(f"{path}: unterminated YAML frontmatter")
    raw_frontmatter: object = yaml.safe_load(parts[1])
    if not isinstance(raw_frontmatter, dict):
        raise ValueError(f"{path}: YAML frontmatter must be a mapping")
    frontmatter = cast(dict[str, object], raw_frontmatter)
    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"{path}: frontmatter description must be a non-empty string")
    return description.strip(), parts[2].lstrip("\r\n").strip()


def _workflow_names() -> set[str]:
    workflow_dir = ROOT / ".agent" / "workflows"
    return {path.stem for path in workflow_dir.glob("gemini-*.md") if path.is_file()}


def _render_command_toml(description: str, prompt: str) -> str:
    """Render valid TOML using JSON-compatible escaped basic strings."""
    return (
        f"description = {json.dumps(description, ensure_ascii=False)}\n"
        f"prompt = {json.dumps(prompt, ensure_ascii=False)}\n"
    )


def check_model_parity() -> list[str]:
    errors: list[str] = []
    try:
        canonical = (ROOT / MODEL_FILE).read_text(encoding="utf-8").strip()
    except OSError as exc:
        return [f"Cannot read canonical model file {MODEL_FILE}: {exc}"]
    if not canonical:
        errors.append(f"Canonical model file {MODEL_FILE} is empty")
    try:
        agent_data: object = json.loads((ROOT / AGENT_JSON).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return errors + [f"Cannot read agent configuration {AGENT_JSON}: {exc}"]
    if not isinstance(agent_data, dict):
        return errors + [f"{AGENT_JSON} must contain a JSON object"]
    agent = cast(dict[str, object], agent_data)
    actual = agent.get("model")
    if actual != canonical:
        errors.append(f"agent.json model {actual!r} != canonical model {canonical!r} (source: {MODEL_FILE})")
    return errors


def check_github_action_model_wiring() -> list[str]:
    """Ensure Gemini workflows load their model from tools/gemini_model."""
    workflow_dir = ROOT / ".github" / "workflows"
    required = (
        "uses: 'actions/checkout@v7'",
        "id: 'gemini_model'",
        "IFS= read -r model < tools/gemini_model",
        "uses: 'google-github-actions/run-gemini-cli@v0'",
        "gemini_model: '${{ steps.gemini_model.outputs.model }}'",
    )
    errors: list[str] = []
    for name in sorted(_workflow_names()):
        workflow_path = workflow_dir / f"{name}.yml"
        try:
            content = workflow_path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"Missing or unreadable GitHub Actions workflow {workflow_path}: {exc}")
            continue
        missing = [item for item in required if item not in content]
        if missing:
            errors.append(
                f"{workflow_path.relative_to(ROOT)} is missing canonical model wiring: {', '.join(missing)}"
            )
            continue
        if not (
            content.index("actions/checkout@v7")
            < content.index("id: 'gemini_model'")
            < content.index("run-gemini-cli@v0")
        ):
            errors.append(f"{workflow_path.relative_to(ROOT)} must load tools/gemini_model before invoking Gemini CLI")
    return errors

def check_prompt_parity() -> list[str]:
    errors: list[str] = []
    workflow_dir = ROOT / ".agent" / "workflows"
    command_dir = ROOT / ".github" / "commands"
    workflow_names = _workflow_names()
    command_names = {path.stem for path in command_dir.glob("gemini-*.toml") if path.is_file()}
    if not workflow_names:
        errors.append(f"No Gemini workflow Markdown files found in {workflow_dir}")
    for name in sorted(workflow_names - command_names):
        errors.append(f"Missing command TOML for workflow: {name}")
    for name in sorted(command_names - workflow_names):
        errors.append(f"Orphan command TOML without workflow Markdown: {name}")
    for name in sorted(workflow_names & command_names):
        md_path = workflow_dir / f"{name}.md"
        toml_path = command_dir / f"{name}.toml"
        try:
            md_description, md_prompt = _parse_md(md_path)
            with toml_path.open("rb") as file_handle:
                toml_data: object = tomllib.load(file_handle)
        except (OSError, tomllib.TOMLDecodeError, yaml.YAMLError, ValueError) as exc:
            errors.append(f"{name}: cannot parse workflow pair: {exc}")
            continue
        if not isinstance(toml_data, dict):
            errors.append(f"{toml_path}: TOML root must be a table")
            continue
        command = cast(dict[str, object], toml_data)
        toml_description = command.get("description")
        toml_prompt = command.get("prompt")
        if not isinstance(toml_description, str):
            errors.append(f"{toml_path}: description must be a string")
        elif md_description != toml_description:
            errors.append(
                f"{name}: description mismatch\n"
                f"  .md  description: {md_description!r}\n"
                f"  .toml description: {toml_description!r}"
            )
        if not isinstance(toml_prompt, str):
            errors.append(f"{toml_path}: prompt must be a string")
        elif md_prompt != toml_prompt.strip():
            errors.append(f"{name}: prompt mismatch between {md_path} and {toml_path}")
    return errors


def check_rules_parity() -> list[str]:
    canonical_path = ROOT / "GEMINI.md"
    if not canonical_path.is_file():
        return [f"Canonical rules file missing: {canonical_path}"]
    try:
        canonical = canonical_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return [f"Cannot read canonical rules file {canonical_path}: {exc}"]
    errors: list[str] = []
    for relative_path, expected_target in SYMLINKS.items():
        target = ROOT / relative_path
        if not target.is_symlink():
            errors.append(f"{relative_path} must be a symlink to {expected_target}")
            continue
        try:
            actual_target = os.readlink(target)
            if actual_target != expected_target:
                errors.append(f"{relative_path} points to {actual_target!r}, expected {expected_target!r}")
                continue
            if target.read_text(encoding="utf-8").strip() != canonical:
                errors.append(f"Rules mismatch: {relative_path} does not resolve to GEMINI.md")
        except OSError as exc:
            errors.append(f"Cannot read symlink {relative_path}: {exc}")
    for relative_path in GENERATED_AGENT_PROFILES:
        profile = ROOT / relative_path
        try:
            if profile.is_symlink():
                errors.append(f"{relative_path} must be a generated regular file, not a symlink")
            elif profile.read_text(encoding="utf-8").strip() != canonical:
                errors.append(f"Generated agent profile is stale: {relative_path}")
        except OSError as exc:
            errors.append(f"Cannot read generated agent profile {relative_path}: {exc}")
    agent_path = ROOT / AGENT_JSON
    try:
        agent_data: object = json.loads(agent_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"Cannot read agent instructions from {AGENT_JSON}: {exc}")
    else:
        if not isinstance(agent_data, dict):
            errors.append(f"{AGENT_JSON} must contain a JSON object")
        else:
            agent = cast(dict[str, object], agent_data)
            instructions = agent.get("instructions")
            if not isinstance(instructions, str) or instructions.strip() != canonical:
                errors.append(f"Rules mismatch: {AGENT_JSON} instructions do not match GEMINI.md")
    obsolete = ROOT / OBSOLETE_COPILOT_FILE
    if obsolete.exists() or obsolete.is_symlink():
        errors.append(f"Obsolete duplicate exists: {OBSOLETE_COPILOT_FILE}; remove the duplicate file")
    return errors


def sync_rules_to_agent_json() -> bool:
    """Synchronize agent.json instructions from canonical GEMINI.md."""
    canonical_path = ROOT / "GEMINI.md"
    agent_path = ROOT / AGENT_JSON
    try:
        canonical = canonical_path.read_text(encoding="utf-8")
        agent_data: object = json.loads(agent_path.read_text(encoding="utf-8"))
        if not isinstance(agent_data, dict):
            raise ValueError(f"{AGENT_JSON} must contain a JSON object")
        agent = cast(dict[str, object], agent_data)
        agent["instructions"] = canonical
        agent_path.write_text(json.dumps(agent, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Failed to synchronize {agent_path}: {exc}", file=sys.stderr)
        return False
    print(f" Synchronized {AGENT_JSON} from GEMINI.md")
    return True


def sync_generated_agent_profiles() -> bool:
    """Generate tool-specific agent discovery files from GEMINI.md."""
    try:
        canonical = (ROOT / "GEMINI.md").read_text(encoding="utf-8")
        for relative_path in GENERATED_AGENT_PROFILES:
            profile = ROOT / relative_path
            profile.parent.mkdir(parents=True, exist_ok=True)
            profile.write_text(canonical, encoding="utf-8")
            print(f" Generated {relative_path}")
    except OSError as exc:
        print(f"Failed to generate agent profiles: {exc}", file=sys.stderr)
        return False
    return True


def sync_workflow_commands() -> bool:
    """Generate GitHub command TOML files from canonical workflow Markdown."""
    workflow_dir = ROOT / ".agent" / "workflows"
    command_dir = ROOT / ".github" / "commands"
    try:
        command_dir.mkdir(parents=True, exist_ok=True)
        workflow_names = _workflow_names()
        if not workflow_names:
            raise ValueError(f"No Gemini workflow Markdown files found in {workflow_dir}")
        for name in sorted(workflow_names):
            description, prompt = _parse_md(workflow_dir / f"{name}.md")
            (command_dir / f"{name}.toml").write_text(
                _render_command_toml(description, prompt),
                encoding="utf-8",
            )
            print(f" Generated .github/commands/{name}.toml")
    except (OSError, yaml.YAMLError, ValueError) as exc:
        print(f"Failed to generate GitHub command TOML files: {exc}", file=sys.stderr)
        return False
    return True


def sync_generated_files() -> list[str]:
    """Synchronize generated copies; symlinks are versioned directly in Git."""
    errors: list[str] = []
    if not sync_generated_agent_profiles():
        errors.append("Failed to generate agent discovery profiles")
    if not sync_rules_to_agent_json():
        errors.append("Failed to synchronize agent.json")
    if not sync_workflow_commands():
        errors.append("Failed to generate GitHub command TOML files")
    return errors


app = typer.Typer(
    help="Validate and synchronize Gemini agent profiles, workflow commands, and model configuration.",
    add_completion=False,
)


@app.command()
def main(
    fix: Annotated[
        bool,
        typer.Option("--fix", "-f", help="Regenerate agent profiles, agent.json instructions, and command TOML files"),
    ] = False,
) -> None:
    errors = sync_generated_files() if fix else []
    errors.extend(check_model_parity())
    errors.extend(check_prompt_parity())
    errors.extend(check_github_action_model_wiring())
    errors.extend(check_rules_parity())
    if errors:
        print("PARITY FAILURES:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        raise typer.Exit(code=1)
    print("All agent parity checks passed (rules, profiles, model, and workflow-command pairs).")

if __name__ == "__main__":
    app()
