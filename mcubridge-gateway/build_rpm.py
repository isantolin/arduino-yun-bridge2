#!/usr/bin/env python3
"""[MIL-SPEC/SIL-2] Self-contained RPM package builder for mcubridge-gateway."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    help="Build RPM package for mcubridge-gateway.",
    add_completion=False,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY_DIR = REPO_ROOT / "mcubridge-gateway"
BIN_DIR = REPO_ROOT / "bin"


def generate_spec_file(version: str, spec_path: Path) -> None:
    spec_content = f"""Name:           mcubridge-gateway
Version:        {version}
Release:        1%{{?dist}}
Summary:        Protobuf Cloud Gateway service (gRPC over HTTP/2) for MCU Bridge v2
License:        GPLv3+
URL:            https://github.com/ignaciosantolin/arduino-yun-bridge2
Source0:        mcubridge-gateway-{version}.tar.gz
BuildArch:      noarch
BuildRequires:  python3-devel
Requires:       python3
Requires:       python3-protobuf
Requires:       python3-cryptography
Requires:       python3-grpclib
Requires:       python3-prometheus-client

%description
Protobuf Cloud Gateway service (gRPC over HTTP/2) for MCU Bridge v2.

%prep
%setup -q

%build
# Compile python bytecode
%py_byte_compile %{{__python3}} mcubridge/

%install
mkdir -p %{{buildroot}}%{{_bindir}}
mkdir -p %{{buildroot}}%{{_unitdir}}
mkdir -p %{{buildroot}}%{{python3_sitelib}}/mcubridge/protocol

# Install executable & service
install -p -m 755 gateway.py %{{buildroot}}%{{_bindir}}/mcubridge-gateway
install -p -m 644 mcubridge-gateway.service %{{buildroot}}%{{_unitdir}}/mcubridge-gateway.service

# Install python modules
cp -p mcubridge/__init__.py %{{buildroot}}%{{python3_sitelib}}/mcubridge/
cp -rp mcubridge/protocol/* %{{buildroot}}%{{python3_sitelib}}/mcubridge/protocol/

%files
%{{_bindir}}/mcubridge-gateway
%{{_unitdir}}/mcubridge-gateway.service
%{{python3_sitelib}}/mcubridge/

%changelog
* Sat Jul 11 2026 Ignacio Santolin <ignacio.santolin@gmail.com> - 2.8.5-1
- Initial package release
"""
    spec_path.write_text(spec_content, encoding="utf-8")


def create_source_tarball(version: str, sources_dir: Path) -> Path:
    temp_src = sources_dir / f"mcubridge-gateway-{version}"
    proto_dest = temp_src / "mcubridge" / "protocol"
    proto_dest.mkdir(parents=True, exist_ok=True)

    shutil.copy2(GATEWAY_DIR / "gateway.py", temp_src / "gateway.py")
    shutil.copy2(GATEWAY_DIR / "mcubridge-gateway.service", temp_src / "mcubridge-gateway.service")

    proto_src = REPO_ROOT / "mcubridge" / "mcubridge" / "protocol"
    for fname in ("mcubridge_pb2.py", "mcubridge_pb2.pyi", "mcubridge_grpc.py"):
        src_f = proto_src / fname
        if src_f.exists():
            shutil.copy2(src_f, proto_dest / fname)

    (temp_src / "mcubridge" / "__init__.py").touch()
    (proto_dest / "__init__.py").touch()

    tarball_path = sources_dir / f"mcubridge-gateway-{version}.tar.gz"
    with tarfile.open(tarball_path, "w:gz") as tar:
        tar.add(temp_src, arcname=f"mcubridge-gateway-{version}")

    shutil.rmtree(temp_src)
    return tarball_path


@app.command()
def main(
    output_dir: Annotated[Path, typer.Option("--output-dir", help="Directory for generated RPM")] = BIN_DIR,
    build_dir: Annotated[Path | None, typer.Option("--build-dir", help="Custom rpmbuild staging directory")] = None,
    keep_build: Annotated[bool, typer.Option("--keep-build", help="Do not delete rpmbuild directory")] = False,
) -> None:
    version_file = REPO_ROOT / "VERSION"
    if not version_file.exists():
        print(f"[ERROR] VERSION file not found at {version_file}", file=sys.stderr)
        sys.exit(1)

    version = version_file.read_text(encoding="utf-8").strip()
    target_build_dir = build_dir if build_dir is not None else GATEWAY_DIR / "rpmbuild"
    sources_dir = target_build_dir / "SOURCES"
    specs_dir = target_build_dir / "SPECS"

    for subdir in ("SOURCES", "SPECS", "BUILD", "RPMS", "SRPMS"):
        (target_build_dir / subdir).mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    create_source_tarball(version, sources_dir)
    spec_path = specs_dir / "mcubridge-gateway.spec"
    generate_spec_file(version, spec_path)

    rpmbuild_bin = shutil.which("rpmbuild")
    if not rpmbuild_bin:
        print("[ERROR] rpmbuild binary not found in PATH", file=sys.stderr)
        sys.exit(1)

    print("[INFO] Running rpmbuild...")
    res = subprocess.run(
        [rpmbuild_bin, "-ba", "--define", f"_topdir {target_build_dir}", str(spec_path)],
        check=False,
    )
    if res.returncode != 0:
        print(f"[ERROR] rpmbuild failed with exit code {res.returncode}", file=sys.stderr)
        sys.exit(res.returncode)

    copied = 0
    for rpm_file in (target_build_dir / "RPMS").rglob("*.rpm"):
        shutil.copy2(rpm_file, output_dir / rpm_file.name)
        copied += 1

    print(f"[INFO] {copied} RPM package(s) copied to {output_dir}")

    if not keep_build:
        shutil.rmtree(target_build_dir, ignore_errors=True)


if __name__ == "__main__":
    app()
