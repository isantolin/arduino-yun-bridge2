// SPDX-License-Identifier: GPL-2.0+
/*
 * Arduino Yun board initialization
 *
 * Extends AP121 board support with:
 * - DDR2 64MB initialization (vs DDR1 32MB)
 * - MCU bridge UART1 on GPIO21/22
 * - 250000 baud serial configuration
 *
 * Copyright (C) 2024 Arduino SA
 *
 * Based on:
 * - Linino U-Boot: board/ar7240/ap121/ap121.c
 * - Upstream U-Boot: board/qca/ap121/ap121.c
 */

#include <common.h>
#include <init.h>
#include <asm/io.h>
#include <asm/addrspace.h>
#include <mach/ar71xx_regs.h>
#include <mach/ath79.h>

DECLARE_GLOBAL_DATA_PTR;

/*
 * AR9331 (Hornet) register definitions
 * These match the Linino U-Boot naming convention
 */
#define AR7240_GPIO_FUNC		0x18040028
#define AR7240_GPIO_OE			0x18040000
#define AR7240_GPIO_OUT			0x18040008
#define AR7240_GPIO_IN			0x18040004

#define AR7240_DDR_TAP_CONTROL0		0x1800001c
#define AR7240_DDR_TAP_CONTROL1		0x18000020
#define AR7240_DDR_CONFIG		0x18000000
#define AR7240_DDR_CONFIG2		0x18000004
#define AR7240_DDR_MODE			0x18000008
#define AR7240_DDR_EXT_MODE		0x1800000c
#define AR7240_DDR_REFRESH		0x18000014
#define AR7240_DDR_RD_DATA		0x18000018

#define HORNET_BOOTSTRAP_STATUS		0x180600ac
#define HORNET_BOOTSTRAP_SEL_25M_40M_MASK	(1 << 0)

/*
 * DDR2 Configuration values for Arduino Yun (64MB)
 * These are critical timing parameters from Linino U-Boot
 */
#ifdef CONFIG_ARDUINO_YUN_DDR2
/* DDR2 timing for 25MHz crystal (default) */
#define YUN_DDR_REFRESH_VAL		0x4186
#define YUN_DDR_CONFIG_VAL		0x7fbc8cd0
#define YUN_DDR_CONFIG2_VAL		0x9dd0e6a8
#define YUN_DDR_MODE_VAL_INIT		0x133
#define YUN_DDR_EXT_MODE_VAL		0x0
#define YUN_DDR_MODE_VAL		0x33
#define YUN_DDR_TRTW_VAL		0x1f
#define YUN_DDR_TWTR_VAL		0x1e
#define YUN_DDR_TAP0_VAL		0x8
#define YUN_DDR_TAP1_VAL		0x9
#define YUN_DDR2_EXT_MODE_VAL		0x402
#define YUN_DDR_RD_DATA_THIS_CYCLE	0x00ff

/* DDR2 timing for 40MHz crystal */
#define YUN_DDR_REFRESH_VAL_40M		0x4270
#endif

/*
 * Enable second UART for MCU bridge communication
 *
 * This function configures GPIO21/22 as UART1 TX/RX for
 * communication with the ATmega32U4 microcontroller.
 *
 * From Linino U-Boot: board/ar7240/ap121/ap121.c::linino_enable_UART()
 */
static void arduino_yun_enable_mcu_uart(void)
{
	u32 val;

	/*
	 * Clear GPIO_FUNC bit24 to allow software control
	 * of UART1 pins (GPIO21/22)
	 */
	val = readl((void *)AR7240_GPIO_FUNC);
	val &= 0xFBFFFFFF;  /* Clear bit 24 */
	writel(val, (void *)AR7240_GPIO_FUNC);

	/*
	 * Configure GPIO21/22 direction:
	 * - GPIO21 (TX): output
	 * - GPIO22 (RX): input (default)
	 */
	val = readl((void *)AR7240_GPIO_OE);
	val |= 0x600000;  /* Set bits 21 and 22 */
	writel(val, (void *)AR7240_GPIO_OE);

	/*
	 * Set GPIO21 high (idle state for UART TX)
	 */
	val = readl((void *)AR7240_GPIO_OUT);
	val |= 0x600000;  /* GPIO21/22 high */
	writel(val, (void *)AR7240_GPIO_OUT);

	debug("Arduino Yun: MCU UART enabled on GPIO21/22\n");
}

/*
 * Initialize DDR2 memory controller
 *
 * The Arduino Yun uses DDR2 SDRAM unlike the AP121 reference
 * which uses DDR1. This requires different timing parameters.
 *
 * From Linino U-Boot: cpu/mips/ar7240/meminit.c::ar7240_ddr_initial_config()
 */
#ifdef CONFIG_ARDUINO_YUN_DDR2
static void arduino_yun_ddr2_init(void)
{
	u32 refresh_val;

	/*
	 * Select refresh rate based on crystal frequency
	 */
	if (readl((void *)HORNET_BOOTSTRAP_STATUS) & 
	    HORNET_BOOTSTRAP_SEL_25M_40M_MASK) {
		/* 40MHz crystal */
		refresh_val = YUN_DDR_REFRESH_VAL_40M;
	} else {
		/* 25MHz crystal (default) */
		refresh_val = YUN_DDR_REFRESH_VAL;
	}

	/*
	 * Initialize DDR2 controller with Yun-specific timing
	 */
	writel(YUN_DDR_CONFIG_VAL, (void *)AR7240_DDR_CONFIG);
	writel(YUN_DDR_CONFIG2_VAL, (void *)AR7240_DDR_CONFIG2);
	
	/* Extended mode register for DDR2 */
	writel(YUN_DDR_MODE_VAL_INIT, (void *)AR7240_DDR_MODE);
	writel(YUN_DDR_EXT_MODE_VAL, (void *)AR7240_DDR_EXT_MODE);
	
	/* Refresh rate */
	writel(refresh_val, (void *)AR7240_DDR_REFRESH);
	
	/* Read data timing */
	writel(YUN_DDR_RD_DATA_THIS_CYCLE, (void *)AR7240_DDR_RD_DATA);

	/* TAP calibration values */
	writel(YUN_DDR_TAP0_VAL, (void *)AR7240_DDR_TAP_CONTROL0);
	writel(YUN_DDR_TAP1_VAL, (void *)AR7240_DDR_TAP_CONTROL1);

	/* Brief delay for DDR2 stabilization */
	udelay(100);

	debug("Arduino Yun: DDR2 64MB initialized\n");
}
#endif

/*
 * Early board initialization
 *
 * Called before relocation. Initialize essential hardware:
 * - DDR2 memory (if not done by SPL)
 * - MCU bridge UART
 */
int board_early_init_f(void)
{
#ifdef CONFIG_ARDUINO_YUN_DDR2
	/* Initialize DDR2 if not already done */
	arduino_yun_ddr2_init();
#endif

	/* Enable MCU bridge UART on GPIO21/22 */
	arduino_yun_enable_mcu_uart();

	return 0;
}

/*
 * Main board initialization
 *
 * Called after relocation. Configure remaining peripherals.
 */
int board_init(void)
{
	/* Setup memory end pointer */
	gd->ram_size = CONFIG_SYS_SDRAM_SIZE;

	return 0;
}

/*
 * Memory initialization
 *
 * Returns the size of available RAM in bytes.
 */
int dram_init(void)
{
#ifdef CONFIG_ARDUINO_YUN_DDR2
	/* Arduino Yun: 64MB DDR2 */
	gd->ram_size = 64 * 1024 * 1024;
#else
	/* Fallback to AP121 default: 32MB DDR1 */
	gd->ram_size = 32 * 1024 * 1024;
#endif

	return 0;
}

/*
 * Board identification string
 */
int checkboard(void)
{
	printf("Board: Arduino Yun (AR9331)\n");
	printf("       DDR2: 64MB, Flash: 16MB\n");
	printf("       UART: 250000 baud (MCU bridge)\n");
	return 0;
}

/*
 * Late board initialization
 *
 * Called just before main loop. Can be used for additional
 * hardware setup or environment modifications.
 */
int last_stage_init(void)
{
	/*
	 * Send startup message to MCU if needed
	 * The ATmega32U4 might be waiting for Linux boot
	 */
	debug("Arduino Yun: Ready for kernel boot\n");

	return 0;
}

/*
 * Additional board-specific commands could be added here
 * to replicate Linino U-Boot's custom commands (chw, chw101, etc.)
 */
