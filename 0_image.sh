#!/bin/bash
set -e
#
# This file is part of Arduino Yun Ecosystem v2.
#
# Copyright (C) 2025 Ignacio Santolin and contributors
#
# 0_image.sh - Compila imagen OpenWrt completa para Arduino Yun
# Target: OpenWrt 25.12.0-rc2 con UART a 115200 baud
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

Compila una imagen OpenWrt completa para Arduino Yun con:
  - UART serial a 115200 baud (en lugar de 250000)
  - Paquetes del ecosistema Yun Bridge preinstalados

Ejemplos:
  ./0_image.sh                  # Usa 25.12.0-rc2 por defecto
  ./0_image.sh 25.12.0-rc2      # Versión específica
  ./0_image.sh v25.12.0-rc2     # Con prefijo 'v'

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

# --- Aplicar parche para 115200 baud ---
DTS_FILE="target/linux/ath79/dts/ar9331_arduino_yun.dts"

if [ -f "$DTS_FILE" ]; then
    log_info "Aplicando parche de baudrate 250000 -> 115200..."
    
    # Backup original
    cp "$DTS_FILE" "${DTS_FILE}.orig"
    
    # Cambiar baudrate en el DTS
    sed -i 's/250000/115200/g' "$DTS_FILE"
    
    # Verificar cambio
    if grep -q "115200" "$DTS_FILE"; then
        log_info "Parche aplicado correctamente al DTS"
    else
        log_warn "No se encontró referencia a baudrate en el DTS (puede estar en otro archivo)"
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

# --- Agregar feed local del proyecto ---
FEEDS_CONF="feeds.conf"
if [ -f "$FEEDS_CONF" ]; then
    # Remover entrada anterior si existe
    sed -i '/yunbridge/d' "$FEEDS_CONF"
fi
echo "src-link yunbridge $SCRIPT_DIR/feeds" >> "$FEEDS_CONF"
log_info "Feed local yunbridge agregado"

./scripts/feeds update yunbridge
./scripts/feeds install -a

# --- Configuración del target ---
log_info "Configurando para Arduino Yun..."

cat > .config <<'DEFCONFIG'
# Target
CONFIG_TARGET_ath79=y
CONFIG_TARGET_ath79_generic=y
CONFIG_TARGET_ath79_generic_DEVICE_arduino-yun=y

# Imagen
CONFIG_TARGET_ROOTFS_SQUASHFS=y
CONFIG_TARGET_ROOTFS_EXT4FS=n

# Paquetes base
CONFIG_PACKAGE_base-files=y
CONFIG_PACKAGE_busybox=y
CONFIG_PACKAGE_dropbear=y

# Python 3.13
CONFIG_PACKAGE_python3=y
CONFIG_PACKAGE_python3-asyncio=y
CONFIG_PACKAGE_python3-logging=y

# MQTT
CONFIG_PACKAGE_mosquitto-ssl=y
CONFIG_PACKAGE_mosquitto-client-ssl=y

# Paquetes Yun Bridge
CONFIG_PACKAGE_openwrt-yun-bridge=y
CONFIG_PACKAGE_openwrt-yun-core=y
CONFIG_PACKAGE_luci-app-yunbridge=y

# Dependencias Python del Bridge
CONFIG_PACKAGE_python3-paho-mqtt=y
CONFIG_PACKAGE_python3-aiomqtt=y
CONFIG_PACKAGE_python3-cobs=y
CONFIG_PACKAGE_python3-prometheus-client=y

# LuCI (opcional pero útil)
CONFIG_PACKAGE_luci=y
CONFIG_PACKAGE_luci-ssl=y

# Utilidades
CONFIG_PACKAGE_htop=y
CONFIG_PACKAGE_nano=y
DEFCONFIG

# Expandir configuración
make defconfig

# --- Compilar ---
NPROC=$(nproc)
log_info "Iniciando compilación con $NPROC threads..."
log_info "Esto puede tomar 30-60 minutos en la primera compilación."

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
    ls -lh "$SYSUPGRADE"
fi

if [ -n "$FACTORY" ]; then
    log_info "Imagen factory: $FACTORY"
    ls -lh "$FACTORY"
fi

echo ""
log_info "Para flashear via sysupgrade (desde el Yun):"
echo "  scp $SYSUPGRADE root@arduino.local:/tmp/"
echo "  ssh root@arduino.local 'sysupgrade -n /tmp/$(basename "$SYSUPGRADE")'"
echo ""
log_info "Para flashear via U-Boot (imagen factory):"
echo "  Consultar: https://openwrt.org/toh/arduino/yun"
