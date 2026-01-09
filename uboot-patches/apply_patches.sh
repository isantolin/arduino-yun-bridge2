#!/bin/bash
# SPDX-License-Identifier: GPL-2.0+
#
# Script para aplicar parches Arduino Yun a U-Boot upstream
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UBOOT_DIR="${1:-}"

usage() {
    echo "Uso: $0 <path-to-uboot>"
    echo ""
    echo "Aplica los parches Arduino Yun al repositorio U-Boot upstream."
    echo ""
    echo "Ejemplo:"
    echo "  git clone https://github.com/u-boot/u-boot.git"
    echo "  $0 ./u-boot"
    exit 1
}

if [ -z "$UBOOT_DIR" ]; then
    usage
fi

if [ ! -f "$UBOOT_DIR/Makefile" ]; then
    echo "Error: $UBOOT_DIR no parece ser un directorio U-Boot válido"
    exit 1
fi

echo "=== Aplicando parches Arduino Yun a U-Boot ==="
echo "Directorio U-Boot: $UBOOT_DIR"
echo ""

# 1. Copiar defconfig
echo "[1/5] Copiando defconfig..."
cp "$SCRIPT_DIR/configs/arduino-yun_defconfig" "$UBOOT_DIR/configs/"

# 2. Copiar Device Tree
echo "[2/5] Copiando Device Tree..."
cp "$SCRIPT_DIR/arch/mips/dts/arduino-yun.dts" "$UBOOT_DIR/arch/mips/dts/"

# 3. Copiar header de configuración
echo "[3/5] Copiando header de configuración..."
cp "$SCRIPT_DIR/include/configs/arduino-yun.h" "$UBOOT_DIR/include/configs/"

# 4. Copiar board code
echo "[4/6] Copiando board code..."
mkdir -p "$UBOOT_DIR/board/arduino/yun"
cp "$SCRIPT_DIR/board/arduino/yun/"* "$UBOOT_DIR/board/arduino/yun/"

# 5. Copiar DDR2 code (reemplaza DDR1 para AR933X)
echo "[5/6] Copiando DDR2 initialization..."
if [ -f "$SCRIPT_DIR/arch/mips/mach-ath79/ar933x/ddr_yun.c" ]; then
    # Backup original DDR code
    if [ -f "$UBOOT_DIR/arch/mips/mach-ath79/ar933x/ddr.c" ]; then
        cp "$UBOOT_DIR/arch/mips/mach-ath79/ar933x/ddr.c" \
           "$UBOOT_DIR/arch/mips/mach-ath79/ar933x/ddr.c.orig"
        echo "  - Backup: ddr.c -> ddr.c.orig"
    fi
    # Replace with DDR2 version
    cp "$SCRIPT_DIR/arch/mips/mach-ath79/ar933x/ddr_yun.c" \
       "$UBOOT_DIR/arch/mips/mach-ath79/ar933x/ddr.c"
    echo "  - Instalado: DDR2 initialization (ddr_yun.c -> ddr.c)"
fi

# 6. Parchear archivos existentes
echo "[6/6] Parcheando archivos de integración..."

# Registrar board en Kconfig de ath79
ATH79_KCONFIG="$UBOOT_DIR/arch/mips/mach-ath79/Kconfig"
if [ -f "$ATH79_KCONFIG" ]; then
    if ! grep -q "arduino/yun" "$ATH79_KCONFIG"; then
        # Añadir después de la línea que contiene ap121
        sed -i '/source "board\/qca\/ap121\/Kconfig"/a source "board/arduino/yun/Kconfig"' "$ATH79_KCONFIG"
        echo "  - Parcheado: arch/mips/mach-ath79/Kconfig"
    else
        echo "  - Ya parcheado: arch/mips/mach-ath79/Kconfig"
    fi
else
    echo "  - ADVERTENCIA: No se encontró $ATH79_KCONFIG"
fi

# Registrar DTS en Makefile
DTS_MAKEFILE="$UBOOT_DIR/arch/mips/dts/Makefile"
if [ -f "$DTS_MAKEFILE" ]; then
    if ! grep -q "arduino-yun.dtb" "$DTS_MAKEFILE"; then
        # Añadir arduino-yun.dtb a la lista de DTBs
        sed -i '/dtb-.*CONFIG_TARGET_AP121/a dtb-$(CONFIG_TARGET_ARDUINO_YUN) += arduino-yun.dtb' "$DTS_MAKEFILE"
        echo "  - Parcheado: arch/mips/dts/Makefile"
    else
        echo "  - Ya parcheado: arch/mips/dts/Makefile"
    fi
else
    echo "  - ADVERTENCIA: No se encontró $DTS_MAKEFILE"
fi

echo ""
echo "=== Parches aplicados correctamente ==="
echo ""
echo "Siguiente paso: compilar U-Boot"
echo ""
echo "  cd $UBOOT_DIR"
echo "  make arduino-yun_defconfig"
echo "  ./scripts/config --disable TOOLS_KWBIMAGE  # Evita dependencia de OpenSSL 3.x"
echo "  ./scripts/config --disable TOOLS_LIBCRYPTO"
echo "  make olddefconfig"
echo "  make CROSS_COMPILE=mips-linux-gnu- -j\$(nproc)"
echo ""
echo "NOTA: Si amlimage.c falla con PRIu8, añadir:"
echo "      sed -i '5 a #include <inttypes.h>' tools/amlimage.c"
echo ""
