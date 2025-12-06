#
# This file is part of Arduino Yun Ecosystem v2.
#
# Copyright (C) 2025 Ignacio Santolin and contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it is useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# OpenWRT package Makefile for openwrt-yun-bridge
# This Makefile declara openwrt-yun-core como dependencia

include $(TOPDIR)/rules.mk

PKG_NAME:=openwrt-yun-bridge
PKG_VERSION:=2.0.0
PKG_RELEASE:=3

PKG_BUILD_DIR := $(BUILD_DIR)/$(PKG_NAME)
HOST_PYTHON3 := $(STAGING_DIR_HOSTPKG)/bin/python3
PYTHON3 := $(if $(wildcard $(HOST_PYTHON3)),$(HOST_PYTHON3),python3)

include $(INCLUDE_DIR)/package.mk

define Package/$(PKG_NAME)
	SECTION:=utils
	CATEGORY:=Utilities
	TITLE:=Yun Bridge Python Daemon
	DEPENDS:=+openwrt-yun-core
	# AUTO-GENERATED RUNTIME DEPENDS BEGIN (run tools/sync_runtime_deps.py)
	DEPENDS+= \
		+python3 \
		+python3-asyncio \
		+python3-uci \
		+python3-pyserial \
		+python3-psutil \
		+python3-more-itertools
	# AUTO-GENERATED RUNTIME DEPENDS END
endef

define Package/$(PKG_NAME)/description
  Python daemon for YunBridge ecosystem. Requires openwrt-yun-core.
endef

define Build/Compile
	$(PYTHON3) -m compileall -q -b $(PKG_BUILD_DIR)
	find $(PKG_BUILD_DIR) -name '__pycache__' -type d -print0 | xargs -0 -r rm -rf
endef

define Build/Prepare
	rm -rf $(PKG_BUILD_DIR)
	mkdir -p $(PKG_BUILD_DIR)
	$(CP) -r ./yunbridge $(PKG_BUILD_DIR)/
	$(CP) ./yunbridge.init $(PKG_BUILD_DIR)/
	$(CP) ./yunbridge.sh $(PKG_BUILD_DIR)/
endef

define Package/$(PKG_NAME)/install
	$(INSTALL_DIR) $(1)/usr/libexec/yunbridge
	$(INSTALL_DIR) $(1)/usr/bin
	$(INSTALL_DIR) $(1)/etc/init.d
	$(CP) $(PKG_BUILD_DIR)/yunbridge $(1)/usr/libexec/yunbridge/
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/yunbridge.sh $(1)/usr/bin/yunbridge
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/yunbridge.init $(1)/etc/init.d/yunbridge
endef

define Package/$(PKG_NAME)/clean
	rm -rf $(PKG_BUILD_DIR)
endef

$(eval $(call BuildPackage,$(PKG_NAME)))
