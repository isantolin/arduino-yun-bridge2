#!/bin/bash
set -e
#
# This file is part of Arduino Yun Ecosystem v2.
#
# Copyright (C) 2025 Ignacio Santolin and contributors
#
# 0_image.sh - Compila imagen OpenWrt completa para Arduino Yun
# Target: OpenWrt 25.12.0-rc2 con UART a 250000 baud (native U-Boot speed)
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/openwrt-build"
OPENWRT_VERSION="${1:-25.12.0-rc2}"
OPENWRT_REPO="https://github.com/openwrt/openwrt.git"

# Colores para output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

usage() {
    cat <<'EOF'
Usage: ./0_image.sh [OPENWRT_VERSION]

Compila una imagen OpenWrt BASE para Arduino Yun con:
  - UART serial a 250000 baud (native, matches U-Boot)
  - Sistema mínimo (~6-8MB, cabe en 16MB flash)

NOTA: Esta imagen NO incluye Python, LuCI ni YunBridge.
      Esos paquetes se instalan después con 2_expand.sh y 3_install.sh.

Flujo completo:
  1. ./0_image.sh        # Compila imagen base
  2. Flashear imagen
  3. ./2_expand.sh       # Configura extroot en SD
  4. ./3_install.sh      # Instala Python, LuCI, YunBridge

Ejemplos:
  ./0_image.sh                  # Usa 25.12.0-rc2 por defecto
  ./0_image.sh 25.12.0-rc2      # Versión específica

Requisitos:
  - ~15GB de espacio en disco
  - ~4GB de RAM
  - Dependencias de build de OpenWrt instaladas

La imagen resultante estará en:
  openwrt-build/bin/targets/ath79/generic/
EOF
}

if [[ "$1" == "-h" || "$1" == "--help" ]]; then
    usage
    exit 0
fi

# Normalizar versión (agregar 'v' si no lo tiene para git checkout)
OPENWRT_TAG="$OPENWRT_VERSION"
if [[ ! "$OPENWRT_TAG" =~ ^v ]]; then
    OPENWRT_TAG="v$OPENWRT_VERSION"
fi

log_info "=== Arduino Yun OpenWrt Image Builder ==="
log_info "Version: $OPENWRT_VERSION (tag: $OPENWRT_TAG)"
log_info "Build dir: $BUILD_DIR"

# --- Verificar dependencias ---
check_command() {
    if ! command -v "$1" &>/dev/null; then
        log_error "Comando requerido no encontrado: $1"
        log_error "Instale las dependencias de build de OpenWrt primero."
        exit 1
    fi
}

log_info "Verificando dependencias..."
for cmd in git make gcc g++ patch wget; do
    check_command "$cmd"
done

# --- Clonar o actualizar OpenWrt ---
if [ -d "$BUILD_DIR/.git" ]; then
    log_info "Directorio de build existente, actualizando..."
    cd "$BUILD_DIR"
    git fetch --all --tags
    git checkout "$OPENWRT_TAG" || git checkout "origin/openwrt-${OPENWRT_VERSION%%.*}.x"
    git pull --ff-only || true
else
    log_info "Clonando OpenWrt..."
    git clone "$OPENWRT_REPO" "$BUILD_DIR"
    cd "$BUILD_DIR"
    git checkout "$OPENWRT_TAG"
fi

# --- Arduino Yun DTS verification (keep native 250000 baud) ---
DTS_FILE="target/linux/ath79/dts/ar9331_arduino_yun.dts"

if [ -f "$DTS_FILE" ]; then
    log_info "Arduino Yun DTS found: $DTS_FILE"
    log_info "Keeping native 250000 baud (experimental branch)"
    
    # Verify baudrate
    if grep -q "250000" "$DTS_FILE"; then
        log_info "DTS baudrate confirmed: 250000 baud"
    else
        log_warn "DTS baudrate check: value may differ from expected"
    fi
else
    log_warn "DTS del Arduino Yun no encontrado en $DTS_FILE"
    log_warn "El archivo puede tener otro nombre en esta versión de OpenWrt"
    
    # Buscar alternativas
    log_info "Buscando archivos DTS del Arduino Yun..."
    find target/linux/ath79 -name "*yun*" -o -name "*arduino*" 2>/dev/null || true
fi

# --- Actualizar feeds ---
log_info "Actualizando feeds..."
./scripts/feeds update -a
./scripts/feeds install -a

# --- Configuración del target (IMAGEN MÍNIMA) ---
log_info "Configurando imagen mínima para Arduino Yun (16MB flash)..."

cat > .config <<'DEFCONFIG'
# Target: Arduino Yun (ath79/generic)
CONFIG_TARGET_ath79=y
CONFIG_TARGET_ath79_generic=y
CONFIG_TARGET_ath79_generic_DEVICE_arduino_yun=y

# Imagen compacta
CONFIG_TARGET_ROOTFS_SQUASHFS=y
CONFIG_TARGET_ROOTFS_EXT4FS=n

# Sistema base (requerido)
CONFIG_PACKAGE_base-files=y
CONFIG_PACKAGE_busybox=y
CONFIG_PACKAGE_dropbear=y

# Soporte extroot (para expandir a SD después)
CONFIG_PACKAGE_block-mount=y
CONFIG_PACKAGE_kmod-fs-ext4=y
CONFIG_PACKAGE_e2fsprogs=y

# Networking básico
CONFIG_PACKAGE_wpad-basic-mbedtls=y

# === OPTIMIZACIÓN DE ESPACIO ===
# Referencia: https://openwrt.org/docs/guide-user/additional-software/saving_space

# Remover PPP (no se usa dial-up en Yun)
# CONFIG_PACKAGE_ppp is not set
# CONFIG_PACKAGE_ppp-mod-pppoe is not set

# NOTA: Python, LuCI, Mosquitto y YunBridge se instalan
# DESPUÉS de configurar extroot con 2_expand.sh y 3_install.sh
# porque no caben en los 16MB de flash del Yun.
DEFCONFIG

# Expandir configuración
make defconfig

# --- Compilar ---
NPROC=$(nproc)
log_info "Iniciando compilación con $NPROC threads..."
log_info "Esto puede tomar 20-40 minutos para imagen mínima."

# Primera pasada: descargar todo
log_info "Descargando fuentes..."
make download -j$NPROC V=s || make download -j1 V=s

# Segunda pasada: compilar
log_info "Compilando imagen..."
if ! make -j$NPROC V=s; then
    log_warn "Build paralelo falló, reintentando con -j1..."
    make -j1 V=s
fi

# --- Resultado ---
IMAGE_DIR="$BUILD_DIR/bin/targets/ath79/generic"
SYSUPGRADE=$(find "$IMAGE_DIR" -name "*arduino*sysupgrade*" -type f 2>/dev/null | head -1)
FACTORY=$(find "$IMAGE_DIR" -name "*arduino*factory*" -type f 2>/dev/null | head -1)

echo ""
log_info "=== Compilación completada ==="
echo ""

if [ -n "$SYSUPGRADE" ]; then
    log_info "Imagen sysupgrade: $SYSUPGRADE"
    SIZE=$(ls -lh "$SYSUPGRADE" | awk '{print $5}')
    log_info "Tamaño: $SIZE (debe ser < 16MB)"
    ls -lh "$SYSUPGRADE"
fi

if [ -n "$FACTORY" ]; then
    log_info "Imagen factory: $FACTORY"
    ls -lh "$FACTORY"
fi

echo ""
log_info "=== Próximos pasos ==="
echo ""
echo "1. Flashear la imagen:"
echo "   scp $SYSUPGRADE root@arduino.local:/tmp/"
echo "   ssh root@arduino.local 'sysupgrade -n /tmp/\$(basename $SYSUPGRADE)'"
echo ""
echo "2. Insertar tarjeta SD y ejecutar:"
echo "   ./2_expand.sh    # Configura extroot en SD"
echo ""
echo "3. Instalar YunBridge:"
echo "   ./3_install.sh   # Instala Python, LuCI, YunBridge"
echo ""
log_warn "IMPORTANTE: Sin tarjeta SD solo tendrás sistema base (SSH)"
