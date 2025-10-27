
include $(TOPDIR)/rules.mk

PKG_NAME:=openwrt-yun-core
PKG_VERSION:=1.0
PKG_RELEASE:=1

include $(INCLUDE_DIR)/package.mk

define Package/$(PKG_NAME)
  SECTION:=utils
  CATEGORY:=Utilities
	TITLE:=Yun Core Scripts and Configs (system Python only)
endef

define Package/$(PKG_NAME)/description
 Core scripts and configuration for Arduino Yun v2. Python dependencies are installed system-wide (no venv).
endef

define Build/Compile
	# Nothing to compile
endef

define Package/$(PKG_NAME)/install
	$(INSTALL_DIR) $(1)/etc/yunbridge
	$(INSTALL_DATA) ./yunbridge.files $(1)/etc/yunbridge/
	$(INSTALL_DIR) $(1)/etc/config
	$(INSTALL_CONF) ./99-yunbridge-ttyath0.conf $(1)/etc/config/yunbridge-ttyath0
	$(INSTALL_DIR) $(1)/www/cgi-bin
	$(INSTALL_DIR) $(1)/usr/bin
	[ -d ./scripts ] && find ./scripts -type f -print0 | xargs -0 -I {} $(INSTALL_BIN) "{}" $(1)/usr/bin/ || true

	# Dummy file to force OpenWRT to use custom postinst
endef

$(eval $(call BuildPackage,openwrt-yun-core))

