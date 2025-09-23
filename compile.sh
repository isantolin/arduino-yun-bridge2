
#!/bin/bash
# compile.sh - Compila todos los paquetes del ecosistema Arduino Yun v2
# Descarga y prepara el buildroot de OpenWRT si es necesario, compila los paquetes OpenWRT y Python, y deja los artefactos listos en bin/
#
# Uso: ./compile.sh
set -e

OPENWRT_VERSION="24.10.3"
OPENWRT_URL="https://downloads.openwrt.org/releases/"$OPENWRT_VERSION"/targets/ath79/generic/openwrt-sdk-"$OPENWRT_VERSION"-ath79-generic_gcc-13.3.0_musl.Linux-x86_64.tar.zst"
SDK_DIR="openwrt-sdk"
BIN_DIR="bin"


echo "[INFO] Installing build dependencies required for OpenWRT SDK (development PC only)"
if [ "$(uname -s)" = "Linux" ]; then
    if [ -f /etc/debian_version ]; then
    echo "[INFO] Installing packages for Ubuntu/Debian..."
        sudo apt-get update
        sudo apt-get install -y build-essential python3 python3-pip python3-venv python3-setuptools python3-wheel python3-build git unzip tar gzip bzip2 xz-utils coreutils libncurses5-dev libncursesw5-dev zstd wget
    elif [ -f /etc/fedora-release ]; then
    echo "[INFO] Installing packages for Fedora..."
        sudo dnf install -y @development-tools python3 python3-pip python3-virtualenv python3-setuptools python3-wheel python3-build git unzip tar gzip bzip2 xz coreutils ncurses-devel zstd wget
    else
    echo "[WARN] Unrecognized Linux distro. Please install manually: build-essential, ncurses-dev, zstd, wget, etc."
    fi
else
    echo "[WARN] Operating system not supported for automatic dependency installation."
fi

echo "[INFO] Preparing build environment..."
mkdir -p "$BIN_DIR"

# 1. Descargar y extraer el buildroot/SDK si no existe
if [ ! -d "$SDK_DIR" ]; then
    echo "[INFO] Descargando OpenWRT SDK..."
    wget -O sdk.tar.zst "$OPENWRT_URL"
    tar --use-compress-program=unzstd -xf sdk.tar.zst
    rm sdk.tar.zst
    mv openwrt-sdk-* "$SDK_DIR"
fi

# 2. Copiar los paquetes OpenWRT al buildroot/SDK
for pkg in luci-app-yunbridge openwrt-yun-core; do
    if [ -d "$pkg" ]; then
    echo "[INFO] Copying $pkg to SDK..."
        rm -rf "$SDK_DIR/package/$pkg"
        # Solo copiar el directorio raíz del paquete, no subdirectorios internos como package/
        cp -r "$pkg" "$SDK_DIR/package/$pkg"
        # Eliminar si accidentalmente se copió package/package
        rm -rf "$SDK_DIR/package/$pkg/package"
    else
    echo "[WARN] Package $pkg not found."
    fi
done

# 3. Compilar los paquetes OpenWRT en el SDK
pushd "$SDK_DIR"
for pkg in luci-app-yunbridge openwrt-yun-core; do
    if [ -d "package/$pkg" ]; then
    echo "[BUILD] Building $pkg (.ipk) in SDK..."
        make package/$pkg/clean V=s || true
        make package/$pkg/compile V=s
        # Copiar artefactos .ipk al bin local
        find bin/packages/ -name "$pkg*_*.ipk" -exec cp {} ../$BIN_DIR/ \;
    fi
done
popd

# 4. Compilar los paquetes Python localmente
for pkg in openwrt-yun-bridge openwrt-yun-client-python; do
    if [ -d "$pkg" ]; then
    echo "[BUILD] Building $pkg (.whl) locally..."
        (cd "$pkg" && make clean && make wheel)
        cp "$pkg"/bin/*.whl "$BIN_DIR/" 2>/dev/null || true
    else
    echo "[WARN] Package $pkg not found."
    fi
done

echo "\n[OK] Build finished. Find the .ipk and .whl artifacts in the bin/ directory."
