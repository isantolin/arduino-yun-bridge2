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
# OpenWRT package Makefile for mcubridge (consolidated daemon + core scripts)

include $(TOPDIR)/rules.mk

PKG_NAME:=mcubridge
PKG_VERSION:=2.8.5
PKG_RELEASE:=1
PKG_LICENSE:=GPL-3.0-or-later
PKG_LICENSE_FILES:=LICENSE
PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>

PKG_BUILD_DIR := $(BUILD_DIR)/$(PKG_NAME)
HOST_PYTHON3 := $(STAGING_DIR_HOSTPKG)/bin/python3
PYTHON3 := $(if $(wildcard $(HOST_PYTHON3)),$(HOST_PYTHON3),python3)

include $(INCLUDE_DIR)/package.mk

define Package/$(PKG_NAME)
	SECTION:=utils
	CATEGORY:=Utilities
	TITLE:=MCU Bridge Daemon and Core Scripts
	PKG_ARCH:=all
	PROVIDES:=mcubridge
	# AUTO-GENERATED RUNTIME DEPENDS BEGIN
	DEPENDS+= \
		python3 \
		python3-aiomqtt \
		python3-paho-mqtt \
		python3-cobs \
		python3-construct \
		python3-msgspec \
		python3-psutil \
		python3-prometheus-client \
		python3-pyserial-asyncio-fast \
		python3-tenacity \
		python3-uvloop \
		python3-cryptography \
		python3-typer \
		python3-structlog \
		python3-rich \
		python3-sh \
		python3-diskcache \
		mosquitto-client \
		python3-svcs \
		python3-click \
		python3-markdown-it-py \
		python3-mdurl \
		python3-pygments \
		python3-shellingham
	# AUTO-GENERATED RUNTIME DEPENDS END
endef

define Package/$(PKG_NAME)/description
  Consolidated Python 3.13 daemon and core scripts for the Arduino MCU Bridge v2.
  Targeting OpenWrt 25.12.0 (APK based) with modern asynchronous I/O and SIL-2 safety.
endef

define Build/Compile
	python3 -O -m compileall -j 0 -b -f -q $(PKG_BUILD_DIR)/mcubridge
	[ ! -d $(PKG_BUILD_DIR)/scripts ] || python3 -m compileall -j 0 -q -b $(PKG_BUILD_DIR)/scripts
	find /tmp -name '__pycache__' -type d -print0 | xargs -0 -r rm -rf || true
endef

define Build/Prepare
	rm -rf $(PKG_BUILD_DIR)
	mkdir -p $(PKG_BUILD_DIR)
	$(CP) -r ./mcubridge $(PKG_BUILD_DIR)/
	find $(PKG_BUILD_DIR)/mcubridge -type d \( -name "tests" -o -name "test" -o -name "tools" -o -name "stubs" -o -name "__pycache__" \) -prune -exec rm -rf {} + || true
	$(CP) ./mcubridge.init $(PKG_BUILD_DIR)/
	$(CP) ./mcubridge.files $(PKG_BUILD_DIR)/
	$(CP) ./uci-defaults/95-mcubridge-silence-kernel-console $(PKG_BUILD_DIR)/
	$(CP) ./uci-defaults/96-mcubridge-flash-protection-paths $(PKG_BUILD_DIR)/
	$(CP) ./uci-defaults/97-mcubridge-default-secret $(PKG_BUILD_DIR)/
	$(CP) ./uci-defaults/98-mcubridge-default-serial $(PKG_BUILD_DIR)/
	$(CP) ./uci-defaults/99-mcubridge-disable-askconsole $(PKG_BUILD_DIR)/
	[ -d ./scripts ] && $(CP) -r ./scripts $(PKG_BUILD_DIR)/ || true
endef

define Package/$(PKG_NAME)/install
	$(INSTALL_DIR) $(1)/usr/libexec/mcubridge
	$(INSTALL_DIR) $(1)/etc/init.d
	$(INSTALL_DIR) $(1)/etc/mcubridge
	$(INSTALL_DIR) $(1)/etc/uci-defaults
	$(INSTALL_DIR) $(1)/usr/bin
	$(INSTALL_DIR) $(1)/www/cgi-bin
	
	# Daemon
	$(CP) $(PKG_BUILD_DIR)/mcubridge/* $(1)/usr/libexec/mcubridge/
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/mcubridge.init $(1)/etc/init.d/mcubridge
	find $(1)/usr/libexec/mcubridge -name "__pycache__" -type d -exec rm -rf {} +
	
	# Core config files
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/mcubridge.files $(1)/etc/mcubridge/
	
	# UCI defaults
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/95-mcubridge-silence-kernel-console $(1)/etc/uci-defaults/
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/96-mcubridge-flash-protection-paths $(1)/etc/uci-defaults/
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/97-mcubridge-default-secret $(1)/etc/uci-defaults/
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/98-mcubridge-default-serial $(1)/etc/uci-defaults/
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/99-mcubridge-disable-askconsole $(1)/etc/uci-defaults/
	
	# Core scripts (install stripping .py extension so command names are unchanged)
	[ -d $(PKG_BUILD_DIR)/scripts ] && \
		for f in $$(find $(PKG_BUILD_DIR)/scripts -type f -name '*.py'); do \
			$(INSTALL_BIN) "$$f" "$(1)/usr/bin/$$(basename $$f .py)"; \
		done || true
endef

define Package/$(PKG_NAME)/clean
	rm -rf $(PKG_BUILD_DIR)
endef

$(eval $(call BuildPackage,$(PKG_NAME)))
