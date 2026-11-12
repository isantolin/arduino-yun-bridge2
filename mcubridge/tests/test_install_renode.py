"""Tests for the pinned Renode installer."""

from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

from tools.ci.install_renode import extract_archive, load_renode_spec, sha256_file


def test_load_renode_spec_reads_pinned_manifest() -> None:
    spec = load_renode_spec()

    assert spec.version in spec.url
    assert len(spec.sha256) == hashlib.sha256().digest_size * 2


def test_sha256_file_matches_standard_digest(tmp_path: Path) -> None:
    payload = b"renode archive integrity"
    archive_path = tmp_path / "archive.tar.gz"
    archive_path.write_bytes(payload)

    assert sha256_file(archive_path) == hashlib.sha256(payload).hexdigest()


def test_extract_archive_removes_single_root_directory(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    executable = staging / "renode" / "renode"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    archive_path = tmp_path / "renode.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(executable.parent, arcname="renode")

    destination = tmp_path / "installed"
    extract_archive(archive_path, destination)

    installed_executable = destination / "renode"
    assert installed_executable.read_text(encoding="utf-8") == "#!/bin/sh\n"
    assert installed_executable.stat().st_mode & 0o111 == 0o111
