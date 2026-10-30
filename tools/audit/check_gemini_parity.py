#!/usr/bin/env python3
"""Validate and synchronize cross-platform agent configuration and Gemini workflows."""

from __future__ import annotations

import json
import re
import shutil
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
)
COPILOT_AGENT_PROFILE = Path(".github/agents/openwrt-architect-arduino.agent.md")
COPILOT_AGENT_TOOLS = ("read", "edit", "search", "execute")
AGENT_JSON = Path(".agent/agents/openwrt-architect-arduino/agent.json")
MODEL_FILE = Path("tools/gemini_model")
CHATGPT_SKILL_EXPORT_SCRIPT = Path("tools/export_chatgpt_skills.py")
CANONICAL_SKILLS_DIR = Path(".agents/skills")
COPILOT_SKILLS_DIR = Path(".github/skills")
REQUIRED_SHARED_SKILLS = (
    "ci-failure-diagnosis",
    "protobuf-protocol-change",
    "embedded-resource-audit",
    "openwrt-runtime-debugging",
    "code-change-verification",
    "security-crypto-review",
    "code-review",
)
COPILOT_SCOPED_INSTRUCTIONS = (
    Path(".github/instructions/python.instructions.md"),
    Path(".github/instructions/firmware.instructions.md"),
    Path(".github/instructions/protocol.instructions.md"),
    Path(".github/instructions/openwrt.instructions.md"),
)
OBSOLETE_COPILOT_FILE = Path(".github/copilot-instructions")


def _parse_frontmatter(path: Path) -> tuple[dict[str, object], str]:
    """Return YAML frontmatter and the remaining Markdown body."""
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---\n"):
        raise ValueError(f"{path}: missing YAML frontmatter")
    parts = content.split("---", 2)
    if len(parts) != 3:
        raise ValueError(f"{path}: unterminated YAML frontmatter")
    raw_frontmatter: object = yaml.safe_load(parts[1])
    if not isinstance(raw_frontmatter, dict):
        raise ValueError(f"{path}: YAML frontmatter must be a mapping")
    return cast(dict[str, object], raw_frontmatter), parts[2].lstrip("\r\n").strip()


def _parse_md(path: Path) -> tuple[str, str]:
    """Return frontmatter description and prompt body from a workflow Markdown file."""
    frontmatter, body = _parse_frontmatter(path)
    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"{path}: frontmatter description must be a non-empty string")
    return description.strip(), body


def _workflow_names() -> set[str]:
    workflow_dir = ROOT / ".agent" / "workflows"
    return {path.stem for path in workflow_dir.glob("gemini-*.md") if path.is_file()}


def _render_command_toml(description: str, prompt: str) -> str:
    """Render valid TOML using JSON-compatible escaped basic strings."""
    return (
        f"description = {json.dumps(description, ensure_ascii=False)}\n"
        f"prompt = {json.dumps(prompt, ensure_ascii=False)}\n"
    )


def check_chatgpt_skill_export() -> list[str]:
    """Ensure the ChatGPT upload packager covers every canonical shared Skill."""
    exporter = ROOT / CHATGPT_SKILL_EXPORT_SCRIPT
    try:
        content = exporter.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"Cannot read ChatGPT Skill exporter {CHATGPT_SKILL_EXPORT_SCRIPT}: {exc}"]
    missing = [name for name in REQUIRED_SHARED_SKILLS if f'"{name}"' not in content]
    if missing:
        return [f"{CHATGPT_SKILL_EXPORT_SCRIPT} does not package shared Skills: {', '.join(missing)}"]
    return []


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
    errors.extend(
        f"Missing command TOML for workflow: {name}"
        for name in sorted(workflow_names - command_names)
    )
    errors.extend(
        f"Orphan command TOML without workflow Markdown: {name}"
        for name in sorted(command_names - workflow_names)
    )
    for name in sorted(workflow_names & command_names):
        md_path = workflow_dir / f"{name}.md"
        toml_path = command_dir / f"{name}.toml"
        try:
            md_description, md_prompt = _parse_md(md_path)
            with toml_path.open("rb") as file_handle:
                command: dict[str, object] = tomllib.load(file_handle)
        except (OSError, tomllib.TOMLDecodeError, yaml.YAMLError, ValueError) as exc:
            errors.append(f"{name}: cannot parse workflow pair: {exc}")
            continue
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


def check_shared_skill_parity() -> list[str]:
    """Validate canonical Skills and their generated Copilot mirrors."""
    errors: list[str] = []
    source_root = ROOT / CANONICAL_SKILLS_DIR
    mirror_root = ROOT / COPILOT_SKILLS_DIR
    if not source_root.is_dir():
        return [f"Canonical shared Skills directory missing: {CANONICAL_SKILLS_DIR}"]

    source_names = {path.name for path in source_root.iterdir() if path.is_dir()}
    for required_name in REQUIRED_SHARED_SKILLS:
        if required_name not in source_names:
            errors.append(f"Required shared Skill missing: {CANONICAL_SKILLS_DIR / required_name / 'SKILL.md'}")

    mirror_names = {path.name for path in mirror_root.iterdir() if path.is_dir()} if mirror_root.is_dir() else set()
    for orphan in sorted(mirror_names - source_names):
        errors.append(f"Orphan generated Copilot Skill mirror: {COPILOT_SKILLS_DIR / orphan}")
    for missing in sorted(source_names - mirror_names):
        errors.append(f"Missing generated Copilot Skill mirror: {COPILOT_SKILLS_DIR / missing}")

    for name in sorted(source_names):
        source_dir = source_root / name
        skill_file = source_dir / "SKILL.md"
        if not skill_file.is_file():
            errors.append(f"Skill directory has no SKILL.md: {skill_file.relative_to(ROOT)}")
            continue
        try:
            metadata, _body = _parse_frontmatter(skill_file)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"Invalid Skill {skill_file.relative_to(ROOT)}: {exc}")
            continue
        skill_name = metadata.get("name")
        description = metadata.get("description")
        if not isinstance(skill_name, str) or skill_name != name:
            errors.append(f"{skill_file.relative_to(ROOT)}: frontmatter name must match directory {name!r}")
        elif re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", skill_name) is None:
            errors.append(f"{skill_file.relative_to(ROOT)}: name must be lowercase kebab-case")
        if not isinstance(description, str) or not description.strip():
            errors.append(f"{skill_file.relative_to(ROOT)}: description must be non-empty")

        mirror_dir = mirror_root / name
        if not mirror_dir.is_dir():
            continue
        source_files = {
            path.relative_to(source_dir).as_posix(): path.read_bytes()
            for path in source_dir.rglob("*")
            if path.is_file()
        }
        mirror_files = {
            path.relative_to(mirror_dir).as_posix(): path.read_bytes()
            for path in mirror_dir.rglob("*")
            if path.is_file()
        }
        if source_files != mirror_files:
            errors.append(f"Generated Copilot Skill mirror is stale: {mirror_dir.relative_to(ROOT)}")
    return errors


def check_copilot_agent_profile() -> list[str]:
    """Validate the Copilot-native agent adapter against canonical instructions."""
    errors: list[str] = []
    profile = ROOT / COPILOT_AGENT_PROFILE
    canonical = ROOT / "GEMINI.md"
    try:
        _canonical_metadata, canonical_body = _parse_frontmatter(canonical)
        metadata, profile_body = _parse_frontmatter(profile)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return [f"Invalid Copilot agent profile {COPILOT_AGENT_PROFILE}: {exc}"]

    if metadata.get("name") != "openwrt-architect-arduino":
        errors.append(f"{COPILOT_AGENT_PROFILE}: invalid or missing agent name")
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.append(f"{COPILOT_AGENT_PROFILE}: description must be non-empty")
    if metadata.get("tools") != list(COPILOT_AGENT_TOOLS):
        errors.append(f"{COPILOT_AGENT_PROFILE}: tools must be {list(COPILOT_AGENT_TOOLS)!r}")
    if profile_body != canonical_body:
        errors.append(f"{COPILOT_AGENT_PROFILE}: prompt body is stale relative to GEMINI.md")
    return errors


def check_copilot_instruction_parity() -> list[str]:
    errors: list[str] = []
    copilot_instructions = ROOT / ".github" / "copilot-instructions.md"
    if not copilot_instructions.is_file():
        errors.append("Missing .github/copilot-instructions.md")
    else:
        try:
            content = copilot_instructions.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"Cannot read {copilot_instructions.relative_to(ROOT)}: {exc}")
        else:
            if ".agents/skills/" not in content or ".github/skills/" not in content:
                errors.append(".github/copilot-instructions.md must identify canonical and mirrored Skill locations")

    for relative_path in COPILOT_SCOPED_INSTRUCTIONS:
        instruction_path = ROOT / relative_path
        try:
            metadata, _body = _parse_frontmatter(instruction_path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"Invalid Copilot scoped instructions {relative_path}: {exc}")
            continue
        apply_to = metadata.get("applyTo")
        if not isinstance(apply_to, str) or not apply_to.strip():
            errors.append(f"{relative_path}: applyTo must be a non-empty string")
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
            actual_target = target.readlink().as_posix()
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
    errors.extend(check_shared_skill_parity())
    errors.extend(check_copilot_agent_profile())
    errors.extend(check_copilot_instruction_parity())
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
    """Generate the Gemini profile and a Copilot-native adapter from GEMINI.md."""
    try:
        canonical_path = ROOT / "GEMINI.md"
        canonical = canonical_path.read_text(encoding="utf-8")
        _metadata, body = _parse_frontmatter(canonical_path)
        for relative_path in GENERATED_AGENT_PROFILES:
            profile = ROOT / relative_path
            profile.parent.mkdir(parents=True, exist_ok=True)
            profile.write_text(canonical, encoding="utf-8")
            print(f" Generated {relative_path}")

        copilot_profile = ROOT / COPILOT_AGENT_PROFILE
        copilot_profile.parent.mkdir(parents=True, exist_ok=True)
        frontmatter = "\n".join(
            (
                "---",
                "name: openwrt-architect-arduino",
                'description: "Principal engineering architect for Arduino MCU Bridge 2, OpenWrt, embedded firmware, Protobuf, daemon, gateway, security, and CI."',
                "tools: [read, edit, search, execute]",
                "---",
                "",
            )
        )
        copilot_profile.write_text(frontmatter + body.rstrip() + "\n", encoding="utf-8")
        print(f" Generated {COPILOT_AGENT_PROFILE}")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Failed to generate agent profiles: {exc}", file=sys.stderr)
        return False
    return True


def sync_generated_skill_mirror() -> bool:
    """Generate Copilot's Skill discovery mirror from canonical shared Skills."""
    source_root = ROOT / CANONICAL_SKILLS_DIR
    mirror_root = ROOT / COPILOT_SKILLS_DIR
    if not source_root.is_dir():
        print(f"Cannot find canonical Skills directory {CANONICAL_SKILLS_DIR}", file=sys.stderr)
        return False
    try:
        mirror_root.mkdir(parents=True, exist_ok=True)
        source_names = {path.name for path in source_root.iterdir() if path.is_dir()}
        for name in sorted(source_names):
            source_dir = source_root / name
            mirror_dir = mirror_root / name
            if mirror_dir.is_symlink():
                mirror_dir.unlink()
            elif mirror_dir.exists():
                shutil.rmtree(mirror_dir)
            shutil.copytree(source_dir, mirror_dir)
            print(f" Mirrored {source_dir.relative_to(ROOT)} -> {mirror_dir.relative_to(ROOT)}")
        for mirror_dir in mirror_root.iterdir():
            if mirror_dir.is_dir() and mirror_dir.name not in source_names:
                shutil.rmtree(mirror_dir)
                print(f" Removed stale generated Skill mirror {mirror_dir.relative_to(ROOT)}")
    except OSError as exc:
        print(f"Failed to synchronize Copilot Skill mirrors: {exc}", file=sys.stderr)
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
    if not sync_generated_skill_mirror():
        errors.append("Failed to generate Copilot Skill mirrors")
    if not sync_workflow_commands():
        errors.append("Failed to generate GitHub command TOML files")
    return errors


app = typer.Typer(
    help="Validate and synchronize cross-platform agent profiles, shared Skills, Gemini workflow commands, and model configuration.",
    add_completion=False,
)


@app.command()
def main(
    fix: Annotated[
        bool,
        typer.Option("--fix", "-f", help="Regenerate agent profiles, agent.json instructions, Copilot Skill mirrors, and command TOML files"),
    ] = False,
) -> None:
    errors = sync_generated_files() if fix else []
    errors.extend(check_chatgpt_skill_export())
    errors.extend(check_model_parity())
    errors.extend(check_prompt_parity())
    errors.extend(check_github_action_model_wiring())
    errors.extend(check_rules_parity())
    if errors:
        print("PARITY FAILURES:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        raise typer.Exit(code=1)
    print("All cross-platform agent parity checks passed (instructions, Skills, profiles, Gemini model, and workflow-command pairs).")


if __name__ == "__main__":
    app()
