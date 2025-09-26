# OpenWRT package Makefile for openwrt-yun-bridge
# This Makefile declara openwrt-yun-core como dependencia

include $(TOPDIR)/rules.mk

PKG_NAME:=openwrt-yun-bridge
PKG_VERSION:=1.0.0
PKG_RELEASE:=1

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
  cp $(PKG_BUILD_DIR)/bridge_daemon.py $(1)/usr/libexec/yunbridge/
  echo '#!/bin/sh\nexec python3 /usr/libexec/yunbridge/bridge_daemon.py "$@"' > $(1)/usr/bin/yunbridge
  chmod 755 $(1)/usr/bin/yunbridge
  cp $(PKG_BUILD_DIR)/yunbridge.init $(1)/etc/init.d/yunbridge
  chmod 755 $(1)/etc/init.d/yunbridge
endef

$(eval $(call BuildPackage,$(PKG_NAME)))
