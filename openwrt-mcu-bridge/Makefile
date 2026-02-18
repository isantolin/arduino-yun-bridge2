#
# This file is part of Arduino MCU Ecosystem v2.
#
# Copyright (C) 2025 Ignacio Santolin and contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it is useful,
# but WITHOUT ANY WARRANTY;
# without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.
# If not, see <https://www.gnu.org/licenses/>.
#
# OpenWRT package Makefile for openwrt-mcu-bridge
# This Makefile declara openwrt-mcu-core como dependencia

include $(TOPDIR)/rules.mk

PKG_NAME:=openwrt-mcu-bridge
PKG_VERSION:=2.5.1
PKG_RELEASE:=2

PKG_BUILD_DIR := $(BUILD_DIR)/$(PKG_NAME)
HOST_PYTHON3 := $(STAGING_DIR_HOSTPKG)/bin/python3
PYTHON3 := $(if $(wildcard $(HOST_PYTHON3)),$(HOST_PYTHON3),python3)

include $(INCLUDE_DIR)/package.mk

define Package/$(PKG_NAME)
	SECTION:=utils
	CATEGORY:=Utilities
	TITLE:=MCU Bridge Python Daemon
	PKG_ARCH:=all
	# [OPENWRT 25.12] Modern apk-based dependencies.
	# We strictly depend on python3 (full) or specific subpackages if optimizing size.
	# High-performance async/struct deps: uvloop, msgspec.
	DEPENDS:=+python3
	# AUTO-GENERATED RUNTIME DEPENDS BEGIN
	DEPENDS+= \
		+python3-aiomqtt \
		+python3-cobs \
		+python3-construct \
		+python3-cryptography \
		+python3-msgspec \
		+python3-paho-mqtt \
		+python3-prometheus-client \
		+python3-psutil \
		+python3-pyserial-asyncio-fast \
		+python3-tenacity \
		+python3-uci \
		+python3-uvloop
	# AUTO-GENERATED RUNTIME DEPENDS END
endef

define Package/$(PKG_NAME)/description
  Modern Python daemon for the Arduino MCU Bridge v2 ecosystem.
  Targeting OpenWrt 25.12+ with full apk support and asynchronous I/O.
endef

define Build/Compile
	# Compile Python source to bytecode to optimize startup time and save RAM
	# on the target.
	# We use -b to generate legacy .pyc files (no __pycache__ dir)
	# which is often preferred for simple packaging, or standard compileall.
	# OpenWrt python3 package usually handles this via hooks, but explicit is safer.
	$(PYTHON3) -m compileall -b -f $(PKG_BUILD_DIR)/mcubridge
endef

define Build/Prepare
	rm -rf $(PKG_BUILD_DIR)
	mkdir -p $(PKG_BUILD_DIR)
	$(CP) -r ./mcubridge $(PKG_BUILD_DIR)/
	# Never ship tests/ or tools/ in production packages (apk/ipk).
	# tools/ contains developer-only stubs (frame_debug) not needed at runtime.
	find $(PKG_BUILD_DIR)/mcubridge -type d \( -name "tests" -o -name "test" -o -name "tools" \) -prune -exec rm -rf {} + || true
	$(CP) ./mcubridge.init $(PKG_BUILD_DIR)/
endef

define Package/$(PKG_NAME)/install
	$(INSTALL_DIR) $(1)/usr/libexec/mcubridge
	$(INSTALL_DIR) $(1)/etc/init.d
	# Copy both source (.py) and bytecode (.pyc)
	$(CP) $(PKG_BUILD_DIR)/mcubridge $(1)/usr/libexec/mcubridge/
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/mcubridge.init $(1)/etc/init.d/mcubridge
	
	# Cleanup any __pycache__ directories if they were created by accident
	find $(1)/usr/libexec/mcubridge -name "__pycache__" -type d -exec rm -rf {} +
endef

define Package/$(PKG_NAME)/clean
	rm -rf $(PKG_BUILD_DIR)
endef

$(eval $(call BuildPackage,$(PKG_NAME)))
