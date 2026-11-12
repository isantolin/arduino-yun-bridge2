#!/usr/bin/env python3
"""Install the checksum-pinned Renode toolchain from the runtime manifest."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "requirements" / "runtime.toml"


@dataclass(frozen=True, slots=True)
class RenodeSpec:
    version: str
    url: str
    sha256: str


def load_renode_spec(manifest_path: Path = MANIFEST_PATH) -> RenodeSpec:
    with manifest_path.open("rb") as manifest_file:
        manifest: dict[str, object] = tomllib.load(manifest_file)
    raw_toolchain = manifest.get("toolchain")
    if not isinstance(raw_toolchain, dict):
        raise ValueError(f"Missing Renode toolchain entry in {manifest_path}")
    toolchain = cast(dict[str, object], raw_toolchain)
    raw_spec = toolchain.get("renode")
    if not isinstance(raw_spec, dict):
        raise ValueError(f"Missing Renode toolchain entry in {manifest_path}")
    spec = cast(dict[str, object], raw_spec)
    version = spec.get("version")
    url = spec.get("url")
    digest = spec.get("sha256")
    if (
        not isinstance(version, str)
        or not version
        or not isinstance(url, str)
        or not url
        or not isinstance(digest, str)
        or not digest
    ):
        raise ValueError(f"Invalid Renode toolchain entry in {manifest_path}")
    if len(digest) != hashlib.sha256().digest_size * 2:
        raise ValueError("Renode SHA-256 must contain 64 hexadecimal characters")
    int(digest, 16)
    if "{version}" not in url:
        raise ValueError("Renode URL must use the manifest version placeholder")
    url = url.format(version=version)
    if version not in url:
        raise ValueError("Renode URL must identify the manifest version")
    return RenodeSpec(version=version, url=url, sha256=digest)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def extract_archive(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mcubridge-renode-") as temporary_dir:
        staging = Path(temporary_dir) / "contents"
        staging.mkdir()
        with tarfile.open(archive_path, "r:gz") as archive:
            archive.extractall(staging, filter="data")
        roots = tuple(staging.iterdir())
        source = roots[0] if len(roots) == 1 and roots[0].is_dir() else staging
        for item in source.iterdir():
            shutil.move(str(item), str(destination / item.name))


def install_renode(destination: Path, manifest_path: Path = MANIFEST_PATH) -> str:
    spec = load_renode_spec(manifest_path)
    with tempfile.TemporaryDirectory(prefix="mcubridge-renode-download-") as temporary_dir:
        archive_path = Path(temporary_dir) / "renode.tar.gz"
        with urlopen(spec.url, timeout=120) as response, archive_path.open("wb") as archive_file:
            shutil.copyfileobj(response, archive_file)
        actual_digest = sha256_file(archive_path)
        if actual_digest != spec.sha256:
            raise ValueError(f"Renode checksum mismatch: expected {spec.sha256}, got {actual_digest}")
        extract_archive(archive_path, destination)
    if github_path := os.environ.get("GITHUB_PATH"):
        with Path(github_path).open("a", encoding="utf-8") as path_file:
            path_file.write(f"{destination}\n")
    return spec.version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(f"Installed Renode {install_renode(args.destination)} into {args.destination}")


if __name__ == "__main__":
    main()
