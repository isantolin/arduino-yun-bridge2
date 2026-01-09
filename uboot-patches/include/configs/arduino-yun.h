/* SPDX-License-Identifier: GPL-2.0+ */
/*
 * Arduino Yun board configuration header
 *
 * Based on AP121 with Linino/Arduino modifications for:
 * - 64MB DDR2 memory (CONFIG_ARDUINO_YUN_DDR2)
 * - 16MB SPI Flash
 * - 250000 baud UART for ATmega32U4 MCU bridge
 * - GPIO21/22 UART1 for MCU communication
 *
 * Copyright (C) 2024 Arduino SA
 */

#ifndef __CONFIG_ARDUINO_YUN_H
#define __CONFIG_ARDUINO_YUN_H

/* Include base AR9331 configuration */
#include <configs/ap121.h>

/* Override board identification */
#undef CONFIG_SYS_BOARD
#define CONFIG_SYS_BOARD		"arduino-yun"

#undef CONFIG_SYS_PROMPT
#define CONFIG_SYS_PROMPT		"arduino> "

/*
 * DDR2 Memory Configuration (override AP121 DDR1 defaults)
 *
 * The Arduino Yun uses Winbond W9751G6KB DDR2 SDRAM (64MB)
 * These values are from the Linino U-Boot configuration.
 */
#define CONFIG_ARDUINO_YUN_DDR2		1

/* DDR2 timing registers - critical for boot */
#define CFG_DDR_REFRESH_VAL		0x4186	/* For 25MHz crystal */
#define CFG_DDR_REFRESH_VAL_40M		0x4270	/* For 40MHz crystal */
#define CFG_DDR_CONFIG_VAL		0x7fbc8cd0
#define CFG_DDR_CONFIG2_VAL		0x9dd0e6a8
#define CFG_DDR_MODE_VAL_INIT		0x133
#define CFG_DDR_EXT_MODE_VAL		0x0
#define CFG_DDR_MODE_VAL		0x33
#define CFG_DDR_TRTW_VAL		0x1f
#define CFG_DDR_TWTR_VAL		0x1e

/* DDR TAP calibration values */
#define CFG_DDR_TAP0_VAL		0x8
#define CFG_DDR_TAP1_VAL		0x9

/* DDR2 extended mode for 64MB */
#define CFG_DDR2_EXT_MODE_VAL		0x402
#define CFG_DDR_RD_DATA_THIS_CYCLE_VAL	0x00ff

/*
 * Memory size - 64MB DDR2
 */
#undef CONFIG_SYS_SDRAM_SIZE
#define CONFIG_SYS_SDRAM_SIZE		(64 * 1024 * 1024)

#undef CONFIG_SYS_MEM_SIZE
#define CONFIG_SYS_MEM_SIZE		64	/* in MB */

/*
 * Flash configuration - 16MB SPI NOR
 *
 * Partition layout:
 *   0x000000-0x040000 : u-boot (256KB)
 *   0x040000-0x050000 : u-boot-env (64KB)
 *   0x050000-0xFF0000 : firmware (rootfs+kernel, ~15.6MB)
 *   0xFF0000-0x1000000 : art/nvram (64KB)
 */
#undef CONFIG_SYS_FLASH_SIZE
#define CONFIG_SYS_FLASH_SIZE		(16 * 1024 * 1024)

#define CONFIG_ENV_OFFSET		0x40000
#define CONFIG_ENV_SIZE			0x10000
#define CONFIG_ENV_SECT_SIZE		0x10000

/* Firmware partition starts after u-boot-env */
#define CONFIG_SYS_FIRMWARE_ADDR	0x9f050000
#define CONFIG_SYS_FIRMWARE_SIZE	0xfa0000

/* ART/calibration data partition */
#define CONFIG_SYS_ART_ADDR		0x9fff0000
#define CONFIG_SYS_ART_SIZE		0x10000

/*
 * Serial/UART configuration
 *
 * UART0: Console + ATmega32U4 MCU bridge at 250000 baud
 * UART1: Alternative MCU communication (GPIO21/22)
 *
 * The non-standard 250000 baud rate is required because:
 * 1. ATmega32U4 runs at 16MHz
 * 2. 250000 gives integer divisor for accurate timing
 * 3. Historical compatibility with Arduino Bridge library
 */
#undef CONFIG_BAUDRATE
#define CONFIG_BAUDRATE			250000

/* Baud rate divisor calculations for AR9331 UART */
/* For 25MHz ref clock: divisor = 25000000 / (16 * 250000) - 1 = 5.25 */
/* For 40MHz ref clock: divisor = 40000000 / (16 * 250000) - 1 = 9 */
#define CFG_UART_BAUDRATE_DIVISOR_25M	6	/* Closest integer */
#define CFG_UART_BAUDRATE_DIVISOR_40M	10

/* Clock step for accurate 250000 baud (from Linino U-Boot) */
#define CFG_UART_CLOCK_STEP_250K	0x7AE0
#define CFG_UART_CLOCK_SCALE_250K	0x0017

/*
 * GPIO configuration for MCU bridge
 *
 * AR7240_GPIO_FUNC register bits:
 * - Clear bit24 to enable GPIO control of UART1 pins
 * - GPIO21 = UART1_TX (to ATmega32U4 RX)
 * - GPIO22 = UART1_RX (from ATmega32U4 TX)
 *
 * AR7240_GPIO_OE register:
 * - Set bit21 as output (TX)
 * - Set bit22 as input (RX)
 */
#define CFG_GPIO_UART1_TX		21
#define CFG_GPIO_UART1_RX		22
#define CFG_GPIO_MCU_HANDSHAKE		27

/* GPIO function register mask for UART1 enable */
#define CFG_GPIO_FUNC_UART1_MASK	0xFBFFFFFF
#define CFG_GPIO_OE_UART1_MASK		0x600000

/*
 * Boot configuration
 */
#define CONFIG_BOOTDELAY		4

#undef CONFIG_BOOTARGS
#define CONFIG_BOOTARGS \
	"console=ttyATH0,250000 root=31:02 rootfstype=squashfs init=/sbin/init"

#undef CONFIG_BOOTCOMMAND
#define CONFIG_BOOTCOMMAND		"bootm 0x9f050000"

/* Autoboot configuration with custom key sequence */
#define CONFIG_AUTOBOOT_KEYED		1
#define CONFIG_AUTOBOOT_PROMPT \
	"autoboot in %d seconds (stop with 'ard')...\n"
#define CONFIG_AUTOBOOT_STOP_STR	"ard"
#define CONFIG_AUTOBOOT_DELAY_STR	"ard"

/*
 * MTD partition string
 */
#define MTDPARTS_DEFAULT \
	"mtdparts=spi0.0:256k(u-boot)ro,64k(u-boot-env),15936k(firmware),64k(art)ro"

/*
 * Environment variables defaults
 */
#define CONFIG_EXTRA_ENV_SETTINGS \
	"board=linino-yun\0" \
	"console=ttyATH0,250000\0" \
	"mtdparts=" MTDPARTS_DEFAULT "\0" \
	"bootcmd=bootm 0x9f050000\0" \
	"bootargs_base=console=${console} root=31:02 rootfstype=squashfs\0" \
	"addparts=setenv bootargs ${bootargs_base} ${mtdparts}\0" \
	"erase_env=sf probe; sf erase 0x40000 0x10000\0"

/*
 * Network configuration
 */
#define CONFIG_ETHADDR			"00:00:00:00:00:00"
#define CONFIG_IPADDR			"192.168.1.1"
#define CONFIG_SERVERIP			"192.168.1.2"
#define CONFIG_NETMASK			"255.255.255.0"

/*
 * Load addresses
 */
#define CONFIG_SYS_LOAD_ADDR		0x81000000
#define CONFIG_SYS_BOOTM_LEN		(16 << 20)	/* 16MB max kernel */

#endif /* __CONFIG_ARDUINO_YUN_H */
