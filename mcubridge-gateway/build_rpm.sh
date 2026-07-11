#!/bin/bash
# Self-contained RPM package builder for mcubridge-gateway
set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GATEWAY_DIR="$REPO_ROOT/mcubridge-gateway"
BUILD_DIR="$GATEWAY_DIR/rpmbuild"
BIN_DIR="$REPO_ROOT/bin"

mkdir -p "$BIN_DIR"
mkdir -p "$BUILD_DIR/SOURCES" "$BUILD_DIR/SPECS" "$BUILD_DIR/BUILD" "$BUILD_DIR/RPMS" "$BUILD_DIR/SRPMS"

# Create source archive
TEMP_SRC="$BUILD_DIR/SOURCES/mcubridge-gateway-2.8.5"
mkdir -p "$TEMP_SRC/mcubridge/protocol"

cp "$GATEWAY_DIR/gateway.py" "$TEMP_SRC/"
cp "$REPO_ROOT/mcubridge/mcubridge/protocol/mcubridge_pb2.py" "$TEMP_SRC/mcubridge/protocol/"
cp "$REPO_ROOT/mcubridge/mcubridge/protocol/mcubridge_pb2.pyi" "$TEMP_SRC/mcubridge/protocol/"
cp "$REPO_ROOT/mcubridge/mcubridge/protocol/mcubridge_grpc.py" "$TEMP_SRC/mcubridge/protocol/"
touch "$TEMP_SRC/mcubridge/__init__.py"
touch "$TEMP_SRC/mcubridge/protocol/__init__.py"

# Tar it up
cd "$BUILD_DIR/SOURCES"
tar -czf mcubridge-gateway-2.8.5.tar.gz mcubridge-gateway-2.8.5
rm -rf mcubridge-gateway-2.8.5
cd "$REPO_ROOT"

# Write spec file
cat << 'EOF' > "$BUILD_DIR/SPECS/mcubridge-gateway.spec"
Name:           mcubridge-gateway
Version:        2.8.5
Release:        1%{?dist}
Summary:        Protobuf Cloud Gateway service (gRPC over HTTP/2) for MCU Bridge v2
License:        GPLv3+
URL:            https://github.com/ignaciosantolin/arduino-yun-bridge2
Source0:        mcubridge-gateway-2.8.5.tar.gz
BuildArch:      noarch
BuildRequires:  python3-devel
Requires:       python3
Requires:       python3-protobuf
Requires:       python3-cryptography
Requires:       python3-grpclib

%description
Protobuf Cloud Gateway service (gRPC over HTTP/2) for MCU Bridge v2.

%prep
%setup -q

%build
# Compile python bytecode
%py_byte_compile %{__python3} mcubridge/

%install
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}%{python3_sitelib}/mcubridge/protocol

# Install executable
install -p -m 755 gateway.py %{buildroot}%{_bindir}/mcubridge-gateway

# Install python modules
cp -p mcubridge/__init__.py %{buildroot}%{python3_sitelib}/mcubridge/
cp -rp mcubridge/protocol/* %{buildroot}%{python3_sitelib}/mcubridge/protocol/

%files
%{_bindir}/mcubridge-gateway
%{python3_sitelib}/mcubridge/

%changelog
* Sat Jul 11 2026 Ignacio Santolin <ignacio.santolin@gmail.com> - 2.8.5-1
- Initial package release
EOF

# Run rpmbuild
echo "[INFO] Running rpmbuild..."
rpmbuild -ba --define "_topdir $BUILD_DIR" "$BUILD_DIR/SPECS/mcubridge-gateway.spec"

# Copy generated RPM to bin/
find "$BUILD_DIR/RPMS" -name "*.rpm" -exec cp {} "$BIN_DIR/" \;
echo "[INFO] RPM package copied to bin/"

# Clean up
rm -rf "$BUILD_DIR"
