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
# Esta imagen incluye:
#   - Baudrate UART corregido a 115200 (en lugar de 250000)
#   - Todos los paquetes del Yun Bridge preinstalados
#   - Scripts de configuración automática (extroot, swap, UCI)
#   - Configuración de seguridad (secretos generados en primer boot)
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/openwrt-build"
FILES_DIR="$BUILD_DIR/files"
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
  - Configuración automática de extroot y swap en primer boot
  - Generación de secretos de seguridad automática

Ejemplos:
  ./0_image.sh                  # Usa 25.12.0-rc2 por defecto
  ./0_image.sh 25.12.0-rc2      # Versión específica

Requisitos:
  - ~15GB de espacio en disco
  - ~4GB de RAM
  - Dependencias de build de OpenWrt instaladas

La imagen resultante estará en:
  openwrt-build/bin/targets/ath79/generic/

Después de flashear:
  1. Insertar tarjeta SD (se configurará automáticamente como extroot)
  2. Conectar por SSH y ejecutar: /etc/init.d/yunbridge status
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
log_info "Buscando y aplicando parche de baudrate..."

# Buscar todos los archivos DTS relacionados con Arduino Yun
DTS_FILES=$(find target/linux/ath79 -name "*.dts" -exec grep -l -i "yun\|arduino" {} \; 2>/dev/null || true)

if [ -n "$DTS_FILES" ]; then
    for DTS_FILE in $DTS_FILES; do
        if grep -q "250000" "$DTS_FILE"; then
            log_info "Aplicando parche de baudrate en: $DTS_FILE"
            cp "$DTS_FILE" "${DTS_FILE}.orig"
            sed -i 's/250000/115200/g' "$DTS_FILE"
        fi
    done
else
    log_warn "No se encontraron archivos DTS del Arduino Yun"
fi

# También buscar en base-files para inittab
INITTAB_FILES=$(find target/linux/ath79 -name "inittab*" 2>/dev/null || true)
for INITTAB in $INITTAB_FILES; do
    if grep -q "250000" "$INITTAB"; then
        log_info "Aplicando parche de baudrate en: $INITTAB"
        sed -i 's/250000/115200/g' "$INITTAB"
    fi
done

# --- Crear directorio files para customizaciones ---
log_info "Creando archivos de configuración personalizados..."
mkdir -p "$FILES_DIR/etc/uci-defaults"
mkdir -p "$FILES_DIR/etc/config"
mkdir -p "$FILES_DIR/usr/bin"

# --- Script de configuración automática de extroot/swap (primer boot) ---
cat > "$FILES_DIR/etc/uci-defaults/99-yunbridge-setup" << 'FIRSTBOOT'
#!/bin/sh
#
# Yun Bridge First Boot Configuration
# Se ejecuta una sola vez después del primer arranque
#

LOG="/tmp/yunbridge-firstboot.log"
echo "=== Yun Bridge First Boot Setup ===" | tee $LOG
date | tee -a $LOG

# --- Generar secretos de seguridad ---
generate_secrets() {
    echo "[INFO] Generating security secrets..." | tee -a $LOG
    
    # Serial shared secret (32 bytes hex)
    SERIAL_SECRET=$(head -c 32 /dev/urandom | hexdump -v -e '/1 "%02x"')
    
    # MQTT password (base64)
    MQTT_PASS=$(head -c 32 /dev/urandom | base64 | tr -d '=' | head -c 43)
    
    # Configurar en UCI
    uci set yunbridge.general=settings
    uci set yunbridge.general.enabled='1'
    uci set yunbridge.general.serial_shared_secret="$SERIAL_SECRET"
    uci set yunbridge.general.mqtt_user='yunbridge'
    uci set yunbridge.general.mqtt_pass="$MQTT_PASS"
    uci set yunbridge.general.serial_baud='115200'
    uci set yunbridge.general.serial_retry_timeout='0.75'
    uci set yunbridge.general.serial_retry_attempts='3'
    uci set yunbridge.general.serial_response_timeout='3.0'
    uci commit yunbridge
    
    echo "[OK] Security secrets generated" | tee -a $LOG
}

# --- Configurar extroot si hay SD disponible ---
setup_extroot() {
    echo "[INFO] Checking for SD card..." | tee -a $LOG
    
    # Detectar dispositivo SD
    SD_DEV=""
    for dev in /dev/mmcblk1p1 /dev/sda1 /dev/sdb1; do
        if [ -b "$dev" ]; then
            SD_DEV="$dev"
            break
        fi
    done
    
    if [ -z "$SD_DEV" ]; then
        echo "[WARN] No SD card detected. Extroot not configured." | tee -a $LOG
        return 1
    fi
    
    echo "[INFO] Found SD at $SD_DEV" | tee -a $LOG
    
    # Verificar si ya está configurado
    if df -k | grep -q "$SD_DEV.*\/overlay"; then
        echo "[OK] Extroot already configured" | tee -a $LOG
        return 0
    fi
    
    # Instalar herramientas necesarias
    opkg update
    opkg install block-mount kmod-fs-ext4 e2fsprogs
    
    # Formatear SD
    echo "[INFO] Formatting $SD_DEV as ext4..." | tee -a $LOG
    umount "$SD_DEV" 2>/dev/null || true
    mkfs.ext4 -F -L extroot "$SD_DEV"
    
    # Obtener UUID
    UUID=$(block info "$SD_DEV" | grep -o -e 'UUID="[^\"]*"' | sed 's/UUID="//;s/"//')
    
    # Configurar fstab
    uci delete fstab.extroot 2>/dev/null || true
    uci set fstab.extroot="mount"
    uci set fstab.extroot.uuid="$UUID"
    uci set fstab.extroot.target="/overlay"
    uci set fstab.extroot.enabled='1'
    uci commit fstab
    
    # Copiar overlay actual
    mkdir -p /mnt/extroot
    mount "$SD_DEV" /mnt/extroot
    tar -C /overlay -cf - . | tar -C /mnt/extroot -xf -
    
    # Crear swapfile de 1GB
    echo "[INFO] Creating 1GB swapfile..." | tee -a $LOG
    dd if=/dev/zero of=/mnt/extroot/swapfile bs=1M count=1024
    mkswap /mnt/extroot/swapfile
    
    # Configurar swap en fstab
    uci delete fstab.swap_file 2>/dev/null || true
    uci set fstab.swap_file="swap"
    uci set fstab.swap_file.device="/overlay/swapfile"
    uci set fstab.swap_file.enabled='1'
    uci commit fstab
    
    umount /mnt/extroot
    
    echo "[OK] Extroot and swap configured. Rebooting..." | tee -a $LOG
    reboot
}

# --- Ejecutar configuración ---
generate_secrets

# Solo intentar extroot si no estamos ya en uno
OVERLAY_SIZE=$(df -k /overlay 2>/dev/null | awk 'NR==2 {print $2}')
if [ "${OVERLAY_SIZE:-0}" -lt 102400 ]; then
    setup_extroot
fi

# Habilitar servicio yunbridge
if [ -x /etc/init.d/yunbridge ]; then
    /etc/init.d/yunbridge enable
    echo "[OK] Yunbridge service enabled" | tee -a $LOG
fi

echo "=== First boot setup complete ===" | tee -a $LOG
exit 0
FIRSTBOOT
chmod +x "$FILES_DIR/etc/uci-defaults/99-yunbridge-setup"

# --- Configuración UCI base para yunbridge ---
cat > "$FILES_DIR/etc/config/yunbridge" << 'UCICONFIG'
config settings 'general'
    option enabled '1'
    option serial_port '/dev/ttyATH0'
    option serial_baud '115200'
    option debug '0'
UCICONFIG

# --- Script helper para rotar credenciales ---
cat > "$FILES_DIR/usr/bin/yunbridge-rotate-credentials" << 'ROTATE'
#!/bin/sh
# Regenera secretos de seguridad del Yun Bridge
set -e

SERIAL_SECRET=$(head -c 32 /dev/urandom | hexdump -v -e '/1 "%02x"')
MQTT_PASS=$(head -c 32 /dev/urandom | base64 | tr -d '=' | head -c 43)

uci set yunbridge.general.serial_shared_secret="$SERIAL_SECRET"
uci set yunbridge.general.mqtt_pass="$MQTT_PASS"
uci commit yunbridge

echo "SERIAL_SECRET=$SERIAL_SECRET"
echo "MQTT_PASS=$MQTT_PASS"
echo ""
echo "Credentials rotated. Restart yunbridge and update your Arduino sketch."
ROTATE
chmod +x "$FILES_DIR/usr/bin/yunbridge-rotate-credentials"

# --- Actualizar feeds ---
log_info "Actualizando feeds..."
./scripts/feeds update -a

# --- Agregar feed local del proyecto ---
FEEDS_CONF="feeds.conf"
if [ -f "$FEEDS_CONF" ]; then
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
CONFIG_PACKAGE_python3-uci=y
CONFIG_PACKAGE_python3-psutil=y

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

# LuCI
CONFIG_PACKAGE_luci=y
CONFIG_PACKAGE_luci-ssl=y
CONFIG_PACKAGE_luci-compat=y
CONFIG_PACKAGE_uhttpd-mod-lua=y

# Extroot support
CONFIG_PACKAGE_block-mount=y
CONFIG_PACKAGE_kmod-fs-ext4=y
CONFIG_PACKAGE_e2fsprogs=y

# Utilidades
CONFIG_PACKAGE_htop=y
CONFIG_PACKAGE_nano=y
CONFIG_PACKAGE_coreutils-stty=y
CONFIG_PACKAGE_openssl-util=y
CONFIG_PACKAGE_avrdude=y
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
log_info "=== Instrucciones de instalación ==="
echo ""
echo "1. Flashear la imagen:"
echo "   scp $SYSUPGRADE root@arduino.local:/tmp/"
echo "   ssh root@arduino.local 'sysupgrade -n /tmp/\$(basename $SYSUPGRADE)'"
echo ""
echo "2. Después del primer boot:"
echo "   - Insertar tarjeta SD (se configura automáticamente como extroot)"
echo "   - El sistema reiniciará una vez para activar extroot"
echo "   - Los secretos de seguridad se generan automáticamente"
echo ""
echo "3. Verificar instalación:"
echo "   ssh root@arduino.local '/etc/init.d/yunbridge status'"
echo "   ssh root@arduino.local 'uci show yunbridge'"
echo ""
echo "4. Obtener el secreto para el sketch Arduino:"
echo "   ssh root@arduino.local 'uci get yunbridge.general.serial_shared_secret'"
