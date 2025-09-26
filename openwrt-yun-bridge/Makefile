# OpenWRT package Makefile for openwrt-yun-bridge
# This Makefile declara openwrt-yun-core como dependencia

include $(TOPDIR)/rules.mk

PKG_NAME:=openwrt-yun-bridge
PKG_VERSION:=2.0.0
PKG_RELEASE:=2

PKG_BUILD_DIR := $(BUILD_DIR)/$(PKG_NAME)

include $(INCLUDE_DIR)/package.mk

define Package/$(PKG_NAME)
	SECTION:=utils
	CATEGORY:=Utilities
	TITLE:=Yun Bridge Python Daemon
	DEPENDS:=+openwrt-yun-core +python3 +python3-pyserial +python3-paho-mqtt
endef

define Package/$(PKG_NAME)/description
  Python daemon for YunBridge ecosystem. Requires openwrt-yun-core.
endef

define Build/Compile
endef

define Package/$(PKG_NAME)/install
	$(INSTALL_DIR) $(1)/usr/libexec/yunbridge
	$(INSTALL_DIR) $(1)/usr/bin
	$(INSTALL_DIR) $(1)/etc/init.d
	# Always copy sources from package dir to build dir (robust to clean)
	cp $(CURDIR)/bridge_daemon.py $(PKG_BUILD_DIR)/
	cp $(CURDIR)/yunbridge.init $(PKG_BUILD_DIR)/
	cp $(PKG_BUILD_DIR)/bridge_daemon.py $(1)/usr/libexec/yunbridge/
	echo -e '#!/bin/sh\nexec python3 /usr/libexec/yunbridge/bridge_daemon.py' > $(1)/usr/bin/yunbridge
	chmod 755 $(1)/usr/bin/yunbridge
	cp $(PKG_BUILD_DIR)/yunbridge.init $(1)/etc/init.d/yunbridge
	chmod 755 $(1)/etc/init.d/yunbridge
endef

$(eval $(call BuildPackage,$(PKG_NAME)))
