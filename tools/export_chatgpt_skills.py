#!/usr/bin/env python3
"""Export repository Agent Skills and their ChatGPT/Codex plugin bundle."""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT / ".agents" / "skills"
REQUIRED_SKILLS = (
    "ci-failure-diagnosis",
    "protobuf-protocol-change",
    "embedded-resource-audit",
    "openwrt-runtime-debugging",
    "code-change-verification",
    "security-crypto-review",
    "code-review",
)
PLUGIN_NAME = "arduino-yun-bridge-engineering"
PLUGIN_VERSION = "1.0.0"
PLUGIN_MANIFEST = {
    "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
    "name": PLUGIN_NAME,
    "version": PLUGIN_VERSION,
    "description": (
        "Seven engineering skills for Arduino MCU Bridge 2: CI, Protobuf, "
        "embedded resources, OpenWrt, verification, security, and code review."
    ),
}


def _required_skill_dirs(skills_root: Path) -> list[tuple[str, Path]]:
    """Resolve every required Skill and fail before creating a partial package."""
    skill_dirs: list[tuple[str, Path]] = []
    for name in REQUIRED_SKILLS:
        skill_dir = skills_root / name
        if not skill_dir.is_dir():
            raise FileNotFoundError(f"Missing shared Skill directory: {skill_dir}")
        if not (skill_dir / "SKILL.md").is_file():
            raise FileNotFoundError(f"Missing required Skill manifest: {skill_dir / 'SKILL.md'}")
        paths = sorted(skill_dir.rglob("*"))
        symlinks = [path for path in paths if path.is_symlink()]
        if symlinks:
            raise ValueError(f"Skill packages must not contain symbolic links: {symlinks[0]}")
        skill_dirs.append((name, skill_dir))
    return skill_dirs


def package_skill(skill_dir: Path, output_dir: Path) -> Path:
    """Create one ZIP with the Skill directory as its top-level folder."""
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        raise FileNotFoundError(f"Missing required Skill manifest: {skill_file}")

    paths = sorted(skill_dir.rglob("*"))
    symlinks = [path for path in paths if path.is_symlink()]
    if symlinks:
        raise ValueError(f"Skill packages must not contain symbolic links: {symlinks[0]}")

    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"{skill_dir.name}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            if not path.is_file():
                continue
            relative_path = path.relative_to(skill_dir).as_posix()
            archive.write(path, f"{skill_dir.name}/{relative_path}")
    return archive_path


def package_plugin(skills_root: Path, output_dir: Path) -> Path:
    """Create one portable Agent Plugin ZIP containing all required shared Skills."""
    skill_dirs = _required_skill_dirs(skills_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"{PLUGIN_NAME}.zip"
    manifest = json.dumps(PLUGIN_MANIFEST, indent=2, ensure_ascii=False) + "\n"

    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{PLUGIN_NAME}/plugin.json", manifest)
        for name, skill_dir in skill_dirs:
            for path in sorted(skill_dir.rglob("*")):
                if not path.is_file():
                    continue
                relative_path = path.relative_to(skill_dir).as_posix()
                archive.write(path, f"{PLUGIN_NAME}/skills/{name}/{relative_path}")
    return archive_path


def export_skills(skills_root: Path, output_dir: Path) -> list[Path]:
    """Package every required shared Skill as an individual ZIP."""
    return [package_skill(skill_dir, output_dir) for _name, skill_dir in _required_skill_dirs(skills_root)]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Export individual ChatGPT Skill ZIPs and one portable ChatGPT/Codex Plugin "
            "containing all shared engineering Skills."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "chatgpt-skills",
        help="Output directory (default: dist/chatgpt-skills)",
    )
    args = parser.parse_args()

    try:
        archives = export_skills(SKILLS_ROOT, args.output)
        plugin_archive = package_plugin(SKILLS_ROOT, args.output)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    for archive in [*archives, plugin_archive]:
        print(archive.relative_to(ROOT) if archive.is_relative_to(ROOT) else archive)
    print(f"Exported {len(archives)} individual Skills and one Plugin to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
