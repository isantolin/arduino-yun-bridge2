#!/usr/bin/env python3
"""Export repository Agent Skills as individual ZIP bundles for ChatGPT Skill upload."""

from __future__ import annotations

import argparse
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


def export_skills(skills_root: Path, output_dir: Path) -> list[Path]:
    """Package every required shared Skill and return the archive paths."""
    archives: list[Path] = []
    for name in REQUIRED_SKILLS:
        skill_dir = skills_root / name
        if not skill_dir.is_dir():
            raise FileNotFoundError(f"Missing shared Skill directory: {skill_dir}")
        archives.append(package_skill(skill_dir, output_dir))
    return archives


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export shared Agent Skills as individual ZIP bundles for the ChatGPT Skills upload interface."
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
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    for archive in archives:
        print(archive.relative_to(ROOT) if archive.is_relative_to(ROOT) else archive)
    print(f"Exported {len(archives)} Skills to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
