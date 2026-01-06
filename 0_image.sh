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
# IMPORTANTE: El Yun tiene solo 16MB de flash. Esta imagen incluye:
#   - Sistema base mínimo (~10-12MB)
#   - Soporte para extroot (block-mount, kmod-fs-ext4)
#   - Script de configuración automática
#
# Los paquetes grandes (Python, LuCI, Mosquitto) se instalan automáticamente
# en el SEGUNDO BOOT después de que extroot esté activo en la tarjeta SD.
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

Compila una imagen OpenWrt para Arduino Yun (16MB flash) con:
  - UART serial a 115200 baud (en lugar de 250000)
  - Soporte para extroot en tarjeta SD
  - Instalación automática de paquetes en segundo boot

Flujo de instalación:
  1. Flashear imagen (~12MB, cabe en 16MB flash)
  2. Insertar tarjeta SD y reiniciar
  3. Primer boot: configura extroot en SD → reinicia automáticamente
  4. Segundo boot: instala Python, LuCI, YunBridge → listo para usar

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
log_warn "Imagen mínima para 16MB flash - paquetes grandes se instalan post-extroot"

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
mkdir -p "$FILES_DIR/etc/yunbridge"

# --- Script de PRIMER BOOT: Solo configura extroot ---
cat > "$FILES_DIR/etc/uci-defaults/50-yunbridge-extroot" << 'FIRSTBOOT'
#!/bin/sh
#
# Yun Bridge First Boot - Fase 1: Configurar Extroot
# Se ejecuta una sola vez después del primer arranque
#

LOG="/tmp/yunbridge-firstboot.log"
MARKER="/etc/yunbridge/.extroot_configured"

echo "=== Yun Bridge First Boot - Fase 1 ===" | tee $LOG
date | tee -a $LOG

# Si ya configuramos extroot, salir
if [ -f "$MARKER" ]; then
    echo "[INFO] Extroot already configured, skipping phase 1" | tee -a $LOG
    exit 0
fi

# --- Configurar extroot si hay SD disponible ---
setup_extroot() {
    echo "[INFO] Checking for SD card..." | tee -a $LOG
    
    # Detectar dispositivo SD
    SD_DEV=""
    for dev in /dev/mmcblk0p1 /dev/mmcblk1p1 /dev/sda1 /dev/sdb1; do
        if [ -b "$dev" ]; then
            SD_DEV="$dev"
            break
        fi
    done
    
    if [ -z "$SD_DEV" ]; then
        echo "[ERROR] No SD card detected!" | tee -a $LOG
        echo "[ERROR] Insert an SD card and reboot to continue setup." | tee -a $LOG
        return 1
    fi
    
    echo "[INFO] Found SD at $SD_DEV" | tee -a $LOG
    
    # Verificar si ya está montado como overlay
    if df -k | grep -q "$SD_DEV.*\/overlay"; then
        echo "[OK] Extroot already active" | tee -a $LOG
        mkdir -p /etc/yunbridge
        touch "$MARKER"
        return 0
    fi
    
    # Formatear SD como ext4
    echo "[INFO] Formatting $SD_DEV as ext4..." | tee -a $LOG
    umount "$SD_DEV" 2>/dev/null || true
    mkfs.ext4 -F -L extroot "$SD_DEV"
    
    # Obtener UUID
    eval $(block info "$SD_DEV" | grep -o -e "UUID=\"[^\"]*\"")
    
    if [ -z "$UUID" ]; then
        echo "[ERROR] Could not get UUID for $SD_DEV" | tee -a $LOG
        return 1
    fi
    
    echo "[INFO] UUID: $UUID" | tee -a $LOG
    
    # Configurar fstab para extroot
    uci -q delete fstab.extroot 2>/dev/null || true
    uci set fstab.extroot="mount"
    uci set fstab.extroot.uuid="$UUID"
    uci set fstab.extroot.target="/overlay"
    uci set fstab.extroot.enabled='1'
    uci commit fstab
    
    # Montar temporalmente y copiar overlay actual
    mkdir -p /mnt/extroot
    mount "$SD_DEV" /mnt/extroot
    
    echo "[INFO] Copying current overlay to SD..." | tee -a $LOG
    tar -C /overlay -cf - . | tar -C /mnt/extroot -xf -
    
    # Crear swapfile de 1GB
    echo "[INFO] Creating 1GB swapfile..." | tee -a $LOG
    dd if=/dev/zero of=/mnt/extroot/swapfile bs=1M count=1024 2>/dev/null
    chmod 600 /mnt/extroot/swapfile
    mkswap /mnt/extroot/swapfile
    
    # Configurar swap en fstab
    uci -q delete fstab.swap_file 2>/dev/null || true
    uci set fstab.swap_file="swap"
    uci set fstab.swap_file.device="/overlay/swapfile"
    uci set fstab.swap_file.enabled='1'
    uci commit fstab
    
    # Marcar como configurado (en la SD que será el nuevo overlay)
    mkdir -p /mnt/extroot/upper/etc/yunbridge
    touch /mnt/extroot/upper/etc/yunbridge/.extroot_configured
    
    umount /mnt/extroot
    
    echo "[OK] Extroot configured. Rebooting to activate..." | tee -a $LOG
    sync
    reboot
}

setup_extroot
exit 0
FIRSTBOOT
chmod +x "$FILES_DIR/etc/uci-defaults/50-yunbridge-extroot"

# --- Script de SEGUNDO BOOT: Instalar paquetes y configurar ---
cat > "$FILES_DIR/etc/uci-defaults/90-yunbridge-install" << 'SECONDBOOT'
#!/bin/sh
#
# Yun Bridge Second Boot - Fase 2: Instalar paquetes
# Se ejecuta después de que extroot está activo
#

LOG="/tmp/yunbridge-install.log"
MARKER="/etc/yunbridge/.packages_installed"
EXTROOT_MARKER="/etc/yunbridge/.extroot_configured"

echo "=== Yun Bridge Second Boot - Fase 2 ===" | tee $LOG
date | tee -a $LOG

# Verificar que extroot esté configurado
if [ ! -f "$EXTROOT_MARKER" ]; then
    echo "[WARN] Extroot not configured yet, skipping package install" | tee -a $LOG
    exit 0
fi

# Si ya instalamos paquetes, salir
if [ -f "$MARKER" ]; then
    echo "[INFO] Packages already installed" | tee -a $LOG
    exit 0
fi

# Verificar que tenemos suficiente espacio (extroot activo)
OVERLAY_SIZE=$(df -k /overlay 2>/dev/null | awk 'NR==2 {print $4}')
if [ "${OVERLAY_SIZE:-0}" -lt 500000 ]; then
    echo "[ERROR] Not enough space in overlay (need 500MB free)" | tee -a $LOG
    echo "[ERROR] Current free: ${OVERLAY_SIZE}KB" | tee -a $LOG
    exit 1
fi

echo "[INFO] Overlay has ${OVERLAY_SIZE}KB free, proceeding..." | tee -a $LOG

# Activar swap
if [ -f /overlay/swapfile ]; then
    echo "[INFO] Activating swap..." | tee -a $LOG
    swapon /overlay/swapfile 2>/dev/null || true
fi

# Actualizar lista de paquetes
echo "[INFO] Updating package lists..." | tee -a $LOG
opkg update

# Remover paquetes conflictivos
echo "[INFO] Removing conflicting packages..." | tee -a $LOG
opkg remove --force-removal-of-dependent-packages ppp ppp-mod-pppoe 2>/dev/null || true

# Instalar paquetes base
echo "[INFO] Installing base packages..." | tee -a $LOG
opkg install coreutils-stty openssl-util ca-certificates

# Instalar Python 3
echo "[INFO] Installing Python 3.13..." | tee -a $LOG
opkg install python3 python3-asyncio python3-logging python3-uci python3-psutil

# Instalar MQTT
echo "[INFO] Installing Mosquitto..." | tee -a $LOG
opkg install mosquitto-ssl mosquitto-client-ssl

# Instalar LuCI
echo "[INFO] Installing LuCI..." | tee -a $LOG
opkg install luci luci-ssl luci-compat uhttpd-mod-lua

# Instalar herramientas
echo "[INFO] Installing utilities..." | tee -a $LOG
opkg install htop nano avrdude

# Instalar dependencias Python del Bridge (desde repos o locales)
echo "[INFO] Installing Python dependencies..." | tee -a $LOG
opkg install python3-paho-mqtt python3-cobs 2>/dev/null || true

# Nota: aiomqtt, prometheus-client pueden no estar en repos oficiales
# Se instalarán con 3_install.sh o desde el feed local

# --- Generar secretos de seguridad ---
echo "[INFO] Generating security secrets..." | tee -a $LOG

SERIAL_SECRET=$(head -c 32 /dev/urandom | hexdump -v -e '/1 "%02x"')
MQTT_PASS=$(head -c 32 /dev/urandom | base64 | tr -d '=' | head -c 43)

uci set yunbridge.general=settings
uci set yunbridge.general.enabled='1'
uci set yunbridge.general.serial_port='/dev/ttyATH0'
uci set yunbridge.general.serial_baud='115200'
uci set yunbridge.general.serial_shared_secret="$SERIAL_SECRET"
uci set yunbridge.general.mqtt_user='yunbridge'
uci set yunbridge.general.mqtt_pass="$MQTT_PASS"
uci set yunbridge.general.serial_retry_timeout='0.75'
uci set yunbridge.general.serial_retry_attempts='3'
uci set yunbridge.general.serial_response_timeout='3.0'
uci set yunbridge.general.debug='0'
uci commit yunbridge

echo "[OK] Security secrets generated" | tee -a $LOG

# Guardar secreto para referencia
cat > /etc/yunbridge/secrets.txt << EOF
# Generated on $(date)
# Use this in your Arduino sketch:
#define BRIDGE_SERIAL_SHARED_SECRET "$SERIAL_SECRET"
EOF
chmod 600 /etc/yunbridge/secrets.txt

# Marcar como completado
touch "$MARKER"

echo "=== Installation complete ===" | tee -a $LOG
echo "" | tee -a $LOG
echo "To complete YunBridge setup:" | tee -a $LOG
echo "1. Transfer project to Yun and run ./3_install.sh" | tee -a $LOG
echo "   (to install yunbridge daemon and dependencies)" | tee -a $LOG
echo "2. Get secret: cat /etc/yunbridge/secrets.txt" | tee -a $LOG
echo "" | tee -a $LOG

exit 0
SECONDBOOT
chmod +x "$FILES_DIR/etc/uci-defaults/90-yunbridge-install"

# --- Configuración UCI base para yunbridge ---
cat > "$FILES_DIR/etc/config/yunbridge" << 'UCICONFIG'
config settings 'general'
    option enabled '0'
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
echo "# Add this to your Arduino sketch:"
echo "#define BRIDGE_SERIAL_SHARED_SECRET \"$SERIAL_SECRET\""
echo ""
echo "Credentials rotated. Restart yunbridge and update your Arduino sketch."
ROTATE
chmod +x "$FILES_DIR/usr/bin/yunbridge-rotate-credentials"

# --- Actualizar feeds ---
log_info "Actualizando feeds..."
./scripts/feeds update -a

# --- Agregar feed local del proyecto (para cuando se corra 3_install.sh) ---
FEEDS_CONF="feeds.conf"
if [ -f "$FEEDS_CONF" ]; then
    sed -i '/yunbridge/d' "$FEEDS_CONF"
fi
echo "src-link yunbridge $SCRIPT_DIR/feeds" >> "$FEEDS_CONF"
log_info "Feed local yunbridge agregado"

./scripts/feeds update yunbridge
./scripts/feeds install -a

# --- Configuración del target (IMAGEN MÍNIMA para 16MB flash) ---
log_info "Configurando imagen mínima para Arduino Yun (16MB flash)..."

cat > .config <<'DEFCONFIG'
# Target: Arduino Yun (ath79/generic)
CONFIG_TARGET_ath79=y
CONFIG_TARGET_ath79_generic=y
CONFIG_TARGET_ath79_generic_DEVICE_arduino-yun=y

# Imagen compacta
CONFIG_TARGET_ROOTFS_SQUASHFS=y
CONFIG_TARGET_ROOTFS_EXT4FS=n

# Sistema base (requerido)
CONFIG_PACKAGE_base-files=y
CONFIG_PACKAGE_busybox=y
CONFIG_PACKAGE_dropbear=y
CONFIG_PACKAGE_uci=y
CONFIG_PACKAGE_opkg=y

# Soporte extroot (CRÍTICO - debe estar en imagen base)
CONFIG_PACKAGE_block-mount=y
CONFIG_PACKAGE_kmod-fs-ext4=y
CONFIG_PACKAGE_e2fsprogs=y
CONFIG_PACKAGE_kmod-usb-storage=y
CONFIG_PACKAGE_kmod-mmc=y

# Networking básico
CONFIG_PACKAGE_wpad-basic-mbedtls=y
CONFIG_PACKAGE_firewall4=y
CONFIG_PACKAGE_nftables=y

# NOTA: Los siguientes paquetes se instalan en SEGUNDO BOOT via opkg
# después de que extroot esté activo (no caben en 16MB flash):
# - python3 (~15MB)
# - luci (~8MB) 
# - mosquitto-ssl (~2MB)
# - htop, nano, avrdude, etc.
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
    log_info "Tamaño: )"
    ls -lh "$SYSUPGRADE"
fi

if [ -n "$FACTORY" ]; then
    log_info "Imagen factory: $FACTORY"
    ls -lh "$FACTORY"
fi

echo ""
log_info "=== Flujo de instalación ==="
echo ""
echo "1. Flashear la imagen:"
echo "   scp $SYSUPGRADE root@arduino.local:/tmp/"
echo "   ssh root@arduino.local 'sysupgrade -n /tmp/\$(basename $SYSUPGRADE)'"
echo ""
echo "2. INSERTAR TARJETA SD y reiniciar el Yun"
echo "   - Primer boot: formatea SD como extroot, crea swap, reinicia"
echo "   - Segundo boot: instala Python, LuCI, Mosquitto (~5-10 min)"
echo ""
echo "3. Transferir proyecto e instalar YunBridge:"
echo "   scp -r . root@arduino.local:/root/yunbridge/"
echo "   ssh root@arduino.local 'cd /root/yunbridge && ./3_install.sh'"
echo ""
echo "4. Obtener el secreto para el sketch Arduino:"
echo "   ssh root@arduino.local 'cat /etc/yunbridge/secrets.txt'"
echo ""
log_warn "IMPORTANTE: Sin tarjeta SD el sistema quedará en modo mínimo"
log_warn "(solo SSH, sin Python/LuCI/YunBridge)"
