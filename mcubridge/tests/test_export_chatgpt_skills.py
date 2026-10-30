"""Tests for ChatGPT Skill ZIP export."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from tools.export_chatgpt_skills import export_skills, package_skill


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


def test_export_skills_requires_all_shared_skills(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    (skills_root / "ci-failure-diagnosis").mkdir(parents=True)
    (skills_root / "ci-failure-diagnosis" / "SKILL.md").write_text(
        "---\nname: ci-failure-diagnosis\ndescription: CI diagnosis\n---\n",
        encoding="utf-8",
    )

    with pytest.raises(FileNotFoundError, match="Missing shared Skill directory"):
        export_skills(skills_root, tmp_path / "output")
