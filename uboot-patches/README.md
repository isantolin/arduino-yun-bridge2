# Arduino Yun U-Boot Patches

Parches incrementales para añadir soporte Arduino Yun al U-Boot upstream moderno.

## Resumen

El Arduino Yun utiliza un SoC AR9331 (Hornet) similar al AP121 de referencia,
pero con diferencias críticas en hardware que requieren configuración específica:

| Característica | AP121 (upstream) | Arduino Yun |
|---------------|------------------|-------------|
| RAM | 32MB DDR1 | **64MB DDR2** |
| Flash | Variable | **16MB SPI** |
| UART Console | 115200 baud | **250000 baud** |
| MCU Bridge | No | **UART1 GPIO21/22** |

## Estructura de Parches

```
uboot-patches/
├── configs/
│   └── arduino-yun_defconfig      # Configuración Kconfig
├── arch/mips/dts/
│   └── arduino-yun.dts            # Device Tree
├── include/configs/
│   └── arduino-yun.h              # Header con constantes DDR2/UART
├── board/arduino/yun/
│   ├── Kconfig                    # Opciones de board
│   ├── Makefile
│   ├── MAINTAINERS
│   └── yun.c                      # Inicialización board (DDR2 + UART)
└── README.md                      # Este archivo
```

## Aplicación de Parches

### 1. Clonar U-Boot upstream

```bash
git clone https://github.com/u-boot/u-boot.git
cd u-boot
git checkout v2024.07  # o versión estable más reciente
```

### 2. Copiar archivos de parche

```bash
# Desde la raíz de arduino-yun-bridge2
UBOOT_DIR=/path/to/u-boot
PATCH_DIR=$(pwd)/uboot-patches

# Copiar defconfig
cp $PATCH_DIR/configs/arduino-yun_defconfig $UBOOT_DIR/configs/

# Copiar Device Tree
cp $PATCH_DIR/arch/mips/dts/arduino-yun.dts $UBOOT_DIR/arch/mips/dts/

# Copiar header de configuración
cp $PATCH_DIR/include/configs/arduino-yun.h $UBOOT_DIR/include/configs/

# Copiar board code
mkdir -p $UBOOT_DIR/board/arduino/yun
cp $PATCH_DIR/board/arduino/yun/* $UBOOT_DIR/board/arduino/yun/
```

### 3. Registrar board en Kconfig

Editar `arch/mips/mach-ath79/Kconfig`:

```kconfig
# Añadir después de TARGET_AP121:
source "board/arduino/yun/Kconfig"
```

### 4. Registrar DTS en Makefile

Editar `arch/mips/dts/Makefile`:

```makefile
# Añadir arduino-yun.dtb a la lista:
dtb-$(CONFIG_TARGET_ARDUINO_YUN) += arduino-yun.dtb
```

### 5. Integrar DDR2 en ar933x

La inicialización DDR2 requiere modificar el código de memoria de AR933X.

**Opción A: Reemplazar ddr.c (recomendado para builds dedicados)**

```bash
# Backup original
mv $UBOOT_DIR/arch/mips/mach-ath79/ar933x/ddr.c \
   $UBOOT_DIR/arch/mips/mach-ath79/ar933x/ddr.c.orig

# Copiar DDR2 con fallback
cp $PATCH_DIR/arch/mips/mach-ath79/ar933x/ddr_yun.c \
   $UBOOT_DIR/arch/mips/mach-ath79/ar933x/ddr.c
```

**Opción B: Build condicional (mantiene compatibilidad)**

Editar `arch/mips/mach-ath79/ar933x/Makefile`:

```makefile
# Original: obj-y += ddr.o
obj-$(CONFIG_ARDUINO_YUN_DDR2) += ddr_yun.o
obj-$(CONFIG_SOC_AR933X) += $(if $(CONFIG_ARDUINO_YUN_DDR2),,ddr.o)
```

### 6. Compilar

```bash
# Configurar
make arduino-yun_defconfig

# Compilar (requiere toolchain MIPS)
make CROSS_COMPILE=mips-linux-gnu- -j$(nproc)

# Output: u-boot.bin
```

## Valores Críticos DDR2

Estos valores fueron extraídos del U-Boot de Linino y son **críticos**
para el arranque correcto. NO modificar sin testing en hardware real.

| Registro | Valor | Descripción |
|----------|-------|-------------|
| `CFG_DDR_CONFIG` | `0x7fbc8cd0` | Timing principal DDR2 |
| `CFG_DDR_CONFIG2` | `0x9dd0e6a8` | Timing secundario |
| `CFG_DDR_REFRESH` | `0x4186` | Refresh rate @25MHz |
| `CFG_DDR_TAP0` | `0x8` | DLL TAP calibración 0 |
| `CFG_DDR_TAP1` | `0x9` | DLL TAP calibración 1 |

## Configuración UART 250000 Baud

El Arduino Yun usa 250000 baud para comunicación con el ATmega32U4.
Esto requiere divisores específicos:

```c
/* Para reloj ref 25MHz */
#define UART_CLOCK_STEP   0x7AE0    /* Step value */
#define UART_CLOCK_SCALE  0x0017    /* Scale value */
/* Fórmula: 25MHz * step / 2^17 / scale ≈ 250000 */
```

### Habilitación UART1 (MCU Bridge)

GPIO21/22 se configuran como UART1 TX/RX:

```c
/* En board_early_init_f() */
val = readl(AR7240_GPIO_FUNC);
val &= 0xFBFFFFFF;  /* Bit 24 = 0: control por software */
writel(val, AR7240_GPIO_FUNC);
```

## Particiones Flash (16MB)

```
0x000000-0x040000 : u-boot     (256KB)
0x040000-0x050000 : u-boot-env (64KB)
0x050000-0xFF0000 : firmware   (15.6MB)
0xFF0000-0x1000000: art        (64KB, calibración WiFi)
```

## Testing

### En QEMU (limitado)

```bash
qemu-system-mips -M malta -kernel u-boot.bin -nographic
# Nota: QEMU no emula AR9331, solo para verificar compilación básica
```

### En Hardware Real

1. Conectar adaptador USB-Serial al conector ISP
2. Configurar terminal: 250000/8N1
3. Flashear via `mtd` desde Linux o via TFTP desde U-Boot existente

```bash
# Desde U-Boot existente:
tftpboot 0x80000000 u-boot.bin
erase 0x9f000000 +0x40000
cp.b 0x80000000 0x9f000000 0x40000
reset
```

## Origen de Valores

Los valores DDR2, timing UART y configuración GPIO fueron extraídos de:

- **Repositorio**: `arduino/uboot-yun`
- **Archivos clave**:
  - `include/configs/ap121.h` (CONFIG_BAUDRATE, DDR config)
  - `cpu/mips/ar7240/hornet_serial.c` (UART 250000 handling)
  - `board/ar7240/ap121/ap121.c` (linino_enable_UART, DDR2 init)

## Compatibilidad

| U-Boot Version | Estado | Notas |
|---------------|--------|-------|
| v2026.01-rc | ✅ **Compilado** | Upstream HEAD enero 2025 |
| v2024.07 | 🟡 Untested | Probablemente compatible |
| v2024.01 | 🟡 Untested | Probablemente compatible |

## Build Verificado

```
$ file u-boot
ELF 32-bit MSB executable, MIPS, MIPS32 rel2 version 1 (SYSV), statically linked

$ ls -la u-boot.bin
-rw-r--r-- 290106 Jan  9 11:15 u-boot.bin

$ grep ddr_tap_tuning u-boot.sym
9f001e28 g     F .text  00000008 ddr_tap_tuning
```

**Toolchain usado**: OpenWrt SDK mips-openwrt-linux-gcc (GCC 14.3.0)

### Notas de Compilación

1. **OpenSSL 3.x incompatibilidad**: Deshabilitar `CONFIG_TOOLS_KWBIMAGE` y
   `CONFIG_TOOLS_LIBCRYPTO` para evitar errores con `openssl/engine.h`:
   ```bash
   ./scripts/config --disable TOOLS_KWBIMAGE
   ./scripts/config --disable TOOLS_LIBCRYPTO
   make olddefconfig
   ```

2. **Bug en amlimage.c** (upstream): Falta `#include <inttypes.h>`:
   ```bash
   sed -i '5 a #include <inttypes.h>' tools/amlimage.c
   ```

3. **Script automatizado**: `apply_patches.sh` incluye instrucciones actualizadas.

## Troubleshooting

### No hay output serial
- Verificar baudrate: debe ser 250000, no 115200
- Verificar conexiones GPIO21/22 si usas puente MCU

### Board no arranca (hang en DDR init)
- Verificar valores `CFG_DDR_*` coinciden exactamente
- Verificar cristal (25MHz vs 40MHz) - afecta refresh rate

### Flash no detectada
- Verificar CONFIG_SPI_FLASH_MACRONIX habilitado
- Verificar chip-select en DTS

## Referencias

- [AR9331 Datasheet](https://www.openwrt.org/docs/techref/hardware/soc/soc.atheros.ar9331)
- [OpenWrt U-Boot AR71xx](https://github.com/pepe2k/u-boot_mod)
- [Linino U-Boot Original](https://github.com/arduino/uboot-yun)

---

Mantenido como parte del proyecto Arduino Yun Bridge 2.
