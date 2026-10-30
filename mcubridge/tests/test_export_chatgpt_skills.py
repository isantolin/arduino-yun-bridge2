"""Tests for individual ChatGPT Skill and combined Plugin ZIP export."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from tools.export_chatgpt_skills import (
    PLUGIN_NAME,
    PLUGIN_MANIFEST,
    REQUIRED_SKILLS,
    export_skills,
    package_plugin,
    package_skill,
)


def _write_required_skills(skills_root: Path) -> None:
    for name in REQUIRED_SKILLS:
        skill_dir = skills_root / name
        metadata = skill_dir / "agents" / "openai.yaml"
        metadata.parent.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Example workflow\n---\n\n# Instructions\n",
            encoding="utf-8",
        )
        metadata.write_text("interface:\n  display_name: Example\n", encoding="utf-8")


def test_package_skill_preserves_skill_root_and_supporting_files(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "example-skill"
    (skill_dir / "references").mkdir(parents=True)
    manifest = "---\nname: example-skill\ndescription: Example skill\n---\n\n# Instructions\n"
    (skill_dir / "SKILL.md").write_text(manifest, encoding="utf-8")
    (skill_dir / "references" / "guide.md").write_text("Reference data\n", encoding="utf-8")

    archive_path = package_skill(skill_dir, tmp_path / "output")

    with zipfile.ZipFile(archive_path) as archive:
        assert sorted(archive.namelist()) == [
            "example-skill/SKILL.md",
            "example-skill/references/guide.md",
        ]
        assert archive.read("example-skill/SKILL.md").decode("utf-8") == manifest
        assert archive.read("example-skill/references/guide.md").decode("utf-8") == "Reference data\n"


def test_package_skill_rejects_symbolic_links(tmp_path: Path) -> None:
    skill_dir = tmp_path / "example-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: example-skill\ndescription: Example\n---\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside.md"
    outside.write_text("must not be packaged\n", encoding="utf-8")
    (skill_dir / "external.md").symlink_to(outside)

    with pytest.raises(ValueError, match="symbolic links"):
        package_skill(skill_dir, tmp_path / "output")


def test_package_plugin_contains_manifest_and_all_skills(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _write_required_skills(skills_root)

    archive_path = package_plugin(skills_root, tmp_path / "output")

    with zipfile.ZipFile(archive_path) as archive:
        plugin_manifest = json.loads(archive.read(f"{PLUGIN_NAME}/plugin.json"))
        assert plugin_manifest == PLUGIN_MANIFEST
        for name in REQUIRED_SKILLS:
            assert f"{PLUGIN_NAME}/skills/{name}/SKILL.md" in archive.namelist()
        assert f"{PLUGIN_NAME}/skills/code-review/agents/openai.yaml" in archive.namelist()


def test_package_plugin_requires_every_shared_skill(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    (skills_root / REQUIRED_SKILLS[0]).mkdir(parents=True)
    (skills_root / REQUIRED_SKILLS[0] / "SKILL.md").write_text(
        "---\nname: ci-failure-diagnosis\ndescription: CI\n---\n",
        encoding="utf-8",
    )

    with pytest.raises(FileNotFoundError, match="Missing shared Skill directory"):
        package_plugin(skills_root, tmp_path / "output")


def test_export_skills_requires_all_shared_skills(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    (skills_root / "ci-failure-diagnosis").mkdir(parents=True)
    (skills_root / "ci-failure-diagnosis" / "SKILL.md").write_text(
        "---\nname: ci-failure-diagnosis\ndescription: CI diagnosis\n---\n",
        encoding="utf-8",
    )

    with pytest.raises(FileNotFoundError, match="Missing shared Skill directory"):
        export_skills(skills_root, tmp_path / "output")
