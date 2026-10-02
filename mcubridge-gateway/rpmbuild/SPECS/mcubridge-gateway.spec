Name:           mcubridge-gateway
Version:        2.8.8
Release:        1%{?dist}
Summary:        Protobuf Cloud Gateway service (gRPC over HTTP/2) for MCU Bridge v2
License:        GPLv3+
URL:            https://github.com/ignaciosantolin/arduino-yun-bridge2
Source0:        mcubridge-gateway-2.8.8.tar.gz
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
%py_byte_compile %{__python3} mcubridge/

%install
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}%{_unitdir}
mkdir -p %{buildroot}%{python3_sitelib}/mcubridge/protocol

# Install executable & service
install -p -m 755 gateway.py %{buildroot}%{_bindir}/mcubridge-gateway
install -p -m 644 mcubridge-gateway.service %{buildroot}%{_unitdir}/mcubridge-gateway.service

# Install python modules
cp -p mcubridge/__init__.py %{buildroot}%{python3_sitelib}/mcubridge/
cp -rp mcubridge/protocol/* %{buildroot}%{python3_sitelib}/mcubridge/protocol/

%files
%{_bindir}/mcubridge-gateway
%{_unitdir}/mcubridge-gateway.service
%{python3_sitelib}/mcubridge/

%changelog
* Sat Jul 11 2026 Ignacio Santolin <ignacio.santolin@gmail.com> - 2.8.5-1
- Initial package release
