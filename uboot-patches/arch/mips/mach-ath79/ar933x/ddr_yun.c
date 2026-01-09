// SPDX-License-Identifier: GPL-2.0+
/*
 * Arduino Yun DDR2 memory initialization for AR9331
 *
 * This file overrides the default DDR1 initialization in
 * arch/mips/mach-ath79/ar933x/ddr.c with DDR2-specific timing
 * for the Arduino Yun's 64MB DDR2 SDRAM.
 *
 * Based on Linino U-Boot: cpu/mips/ar7240/meminit.c
 *
 * Copyright (C) 2024 Arduino SA
 */

#include <asm/global_data.h>
#include <asm/io.h>
#include <asm/addrspace.h>
#include <linux/delay.h>
#include <mach/ar71xx_regs.h>
#include <mach/ath79.h>
#include <mach/ddr.h>

DECLARE_GLOBAL_DATA_PTR;

/*
 * AR9331 DDR Controller registers
 */
#define AR933X_DDR_BASE                 0x18000000
#define AR933X_DDR_CONFIG               0x00
#define AR933X_DDR_CONFIG2              0x04
#define AR933X_DDR_MODE                 0x08
#define AR933X_DDR_EXT_MODE             0x0c
#define AR933X_DDR_CTRL                 0x10
#define AR933X_DDR_REFRESH              0x14
#define AR933X_DDR_RD_DATA              0x18
#define AR933X_DDR_TAP_CTRL0            0x1c
#define AR933X_DDR_TAP_CTRL1            0x20

/* DDR Controller commands */
#define AR933X_DDR_CTRL_PRECHARGE       BIT(3)
#define AR933X_DDR_CTRL_AUTO_REFRESH    BIT(2)
#define AR933X_DDR_CTRL_MODE_REG        BIT(1)
#define AR933X_DDR_CTRL_EXT_MODE_REG    BIT(0)

/* Bootstrap register for crystal detection */
#define AR933X_BOOTSTRAP                0x180600ac
#define AR933X_BOOTSTRAP_REF_CLK_40     BIT(0)

/*
 * DDR2 timing values for Arduino Yun (64MB Winbond W9751G6KB)
 *
 * These values are extracted from Linino U-Boot and are
 * CRITICAL for correct memory operation. Do not modify
 * without hardware testing.
 */

/* DDR2 timing for 25MHz reference crystal (default) */
#define YUN_DDR2_CONFIG_25M             0x7fbc8cd0
#define YUN_DDR2_CONFIG2_25M            0x9dd0e6a8
#define YUN_DDR2_REFRESH_25M            0x4186
#define YUN_DDR2_MODE_INIT              0x133
#define YUN_DDR2_MODE                   0x33
#define YUN_DDR2_EXT_MODE               0x0
#define YUN_DDR2_EXT_MODE2              0x402
#define YUN_DDR2_TAP0                   0x8
#define YUN_DDR2_TAP1                   0x9
#define YUN_DDR2_RD_DATA                0x00ff

/* DDR2 timing for 40MHz reference crystal */
#define YUN_DDR2_REFRESH_40M            0x4270

/*
 * DDR1 timing values (for reference/fallback)
 * These are the original AP121 DDR1 values
 */
#define YUN_DDR1_CONFIG_25M             0xefbc8cd0
#define YUN_DDR1_CONFIG2_25M            0x8e7156a2
#define YUN_DDR1_REFRESH_25M            0x4186
#define YUN_DDR1_MODE                   0x61
#define YUN_DDR1_EXT_MODE               0x0
#define YUN_DDR1_TAP0                   0x8
#define YUN_DDR1_TAP1                   0x9
#define YUN_DDR1_RD_DATA                0xff

/*
 * Initialize DDR2 memory controller
 *
 * This function performs the DDR2 initialization sequence
 * required for the Arduino Yun. Must be called very early
 * in the boot process, before any DRAM access.
 */
void arduino_yun_ddr2_init(void)
{
	void __iomem *ddr_base = (void __iomem *)KSEG1ADDR(AR933X_DDR_BASE);
	void __iomem *bootstrap = (void __iomem *)KSEG1ADDR(AR933X_BOOTSTRAP);
	u32 refresh_val;
	int i;

	/* Determine refresh rate based on crystal frequency */
	if (readl(bootstrap) & AR933X_BOOTSTRAP_REF_CLK_40)
		refresh_val = YUN_DDR2_REFRESH_40M;
	else
		refresh_val = YUN_DDR2_REFRESH_25M;

	/*
	 * DDR2 Initialization Sequence:
	 *
	 * 1. Configure DDR timing registers
	 * 2. Issue PRECHARGE ALL command
	 * 3. Issue EMRS (Extended Mode Register Set) - DLL enable
	 * 4. Issue MRS (Mode Register Set) - DLL reset
	 * 5. Issue PRECHARGE ALL
	 * 6. Issue 2x AUTO REFRESH
	 * 7. Issue MRS without DLL reset
	 * 8. Issue EMRS for OCD calibration
	 * 9. Issue EMRS to exit OCD calibration
	 * 10. Configure TAP values and read data timing
	 */

	/* Step 1: Configure DDR timing */
	writel(YUN_DDR2_CONFIG_25M, ddr_base + AR933X_DDR_CONFIG);
	writel(YUN_DDR2_CONFIG2_25M, ddr_base + AR933X_DDR_CONFIG2);

	/* Step 2: Precharge all banks */
	writel(AR933X_DDR_CTRL_PRECHARGE, ddr_base + AR933X_DDR_CTRL);
	udelay(10);

	/* Step 3: EMRS - Enable DLL */
	writel(YUN_DDR2_EXT_MODE, ddr_base + AR933X_DDR_EXT_MODE);
	writel(AR933X_DDR_CTRL_EXT_MODE_REG, ddr_base + AR933X_DDR_CTRL);
	udelay(10);

	/* Step 4: MRS - DLL reset */
	writel(YUN_DDR2_MODE_INIT, ddr_base + AR933X_DDR_MODE);
	writel(AR933X_DDR_CTRL_MODE_REG, ddr_base + AR933X_DDR_CTRL);
	udelay(10);

	/* Step 5: Precharge all again */
	writel(AR933X_DDR_CTRL_PRECHARGE, ddr_base + AR933X_DDR_CTRL);
	udelay(10);

	/* Step 6: Auto refresh (2 times) */
	for (i = 0; i < 2; i++) {
		writel(AR933X_DDR_CTRL_AUTO_REFRESH, ddr_base + AR933X_DDR_CTRL);
		udelay(10);
	}

	/* Step 7: MRS without DLL reset */
	writel(YUN_DDR2_MODE, ddr_base + AR933X_DDR_MODE);
	writel(AR933X_DDR_CTRL_MODE_REG, ddr_base + AR933X_DDR_CTRL);
	udelay(10);

	/* Step 8: EMRS for OCD default */
	writel(YUN_DDR2_EXT_MODE2, ddr_base + AR933X_DDR_EXT_MODE);
	writel(AR933X_DDR_CTRL_EXT_MODE_REG, ddr_base + AR933X_DDR_CTRL);
	udelay(10);

	/* Step 9: EMRS exit OCD */
	writel(YUN_DDR2_EXT_MODE, ddr_base + AR933X_DDR_EXT_MODE);
	writel(AR933X_DDR_CTRL_EXT_MODE_REG, ddr_base + AR933X_DDR_CTRL);
	udelay(10);

	/* Step 10: Configure refresh, TAP, and read timing */
	writel(refresh_val, ddr_base + AR933X_DDR_REFRESH);
	writel(YUN_DDR2_TAP0, ddr_base + AR933X_DDR_TAP_CTRL0);
	writel(YUN_DDR2_TAP1, ddr_base + AR933X_DDR_TAP_CTRL1);
	writel(YUN_DDR2_RD_DATA, ddr_base + AR933X_DDR_RD_DATA);

	/* Allow time for memory to stabilize */
	udelay(100);
}

/*
 * Initialize DDR1 memory controller (fallback)
 *
 * For boards with DDR1 memory (like original AP121)
 */
void arduino_yun_ddr1_init(void)
{
	void __iomem *ddr_base = (void __iomem *)KSEG1ADDR(AR933X_DDR_BASE);
	void __iomem *bootstrap = (void __iomem *)KSEG1ADDR(AR933X_BOOTSTRAP);
	u32 refresh_val;
	int i;

	if (readl(bootstrap) & AR933X_BOOTSTRAP_REF_CLK_40)
		refresh_val = YUN_DDR2_REFRESH_40M;  /* Same formula */
	else
		refresh_val = YUN_DDR1_REFRESH_25M;

	/* DDR1 timing configuration */
	writel(YUN_DDR1_CONFIG_25M, ddr_base + AR933X_DDR_CONFIG);
	writel(YUN_DDR1_CONFIG2_25M, ddr_base + AR933X_DDR_CONFIG2);

	/* Precharge */
	writel(AR933X_DDR_CTRL_PRECHARGE, ddr_base + AR933X_DDR_CTRL);
	udelay(10);

	/* EMRS */
	writel(YUN_DDR1_EXT_MODE, ddr_base + AR933X_DDR_EXT_MODE);
	writel(AR933X_DDR_CTRL_EXT_MODE_REG, ddr_base + AR933X_DDR_CTRL);
	udelay(10);

	/* MRS */
	writel(YUN_DDR1_MODE, ddr_base + AR933X_DDR_MODE);
	writel(AR933X_DDR_CTRL_MODE_REG, ddr_base + AR933X_DDR_CTRL);
	udelay(10);

	/* Precharge */
	writel(AR933X_DDR_CTRL_PRECHARGE, ddr_base + AR933X_DDR_CTRL);
	udelay(10);

	/* Auto refresh x2 */
	for (i = 0; i < 2; i++) {
		writel(AR933X_DDR_CTRL_AUTO_REFRESH, ddr_base + AR933X_DDR_CTRL);
		udelay(10);
	}

	/* Final MRS */
	writel(YUN_DDR1_MODE, ddr_base + AR933X_DDR_MODE);
	writel(AR933X_DDR_CTRL_MODE_REG, ddr_base + AR933X_DDR_CTRL);
	udelay(10);

	/* Refresh and TAP */
	writel(refresh_val, ddr_base + AR933X_DDR_REFRESH);
	writel(YUN_DDR1_TAP0, ddr_base + AR933X_DDR_TAP_CTRL0);
	writel(YUN_DDR1_TAP1, ddr_base + AR933X_DDR_TAP_CTRL1);
	writel(YUN_DDR1_RD_DATA, ddr_base + AR933X_DDR_RD_DATA);

	udelay(100);
}

/*
 * Hook for U-Boot DDR initialization
 *
 * Called from lowlevel_init.S to initialize DDR memory controller.
 * If CONFIG_ARDUINO_YUN_DDR2 is set, uses DDR2 initialization;
 * otherwise DDR1.
 */
void ddr_init(void)
{
#ifdef CONFIG_ARDUINO_YUN_DDR2
	arduino_yun_ddr2_init();
#else
	arduino_yun_ddr1_init();
#endif
}

/*
 * DDR TAP tuning
 *
 * Called from arch/mips/mach-ath79/dram.c to perform TAP calibration.
 * For Arduino Yun, TAP values are already set during ddr_init(),
 * so this function is a no-op. The upstream dram.c will call this
 * and then use get_ram_size() for auto-detection.
 */
void ddr_tap_tuning(void)
{
	/*
	 * TAP calibration was already done in ddr_init().
	 * TAP0 = 0x8, TAP1 = 0x9 for DDR2
	 * These values are from Linino U-Boot and work reliably.
	 *
	 * A more sophisticated implementation could do dynamic
	 * calibration here, but the static values are proven
	 * to work on Arduino Yun hardware.
	 */
}
