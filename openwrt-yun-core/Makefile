
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
	$(INSTALL_DIR) $(1)/etc/init.d
	$(INSTALL_BIN) ./yunbridge.init $(1)/etc/init.d/yunbridge
	$(INSTALL_DIR) $(1)/www/cgi-bin
	[ -f ./scripts/pin_rest_cgi.py ] && $(INSTALL_BIN) ./scripts/pin_rest_cgi.py $(1)/www/cgi-bin/pin || true
	$(INSTALL_DIR) $(1)/usr/bin
	[ -d ./scripts ] && for f in ./scripts/*; do $(INSTALL_BIN) $$f $(1)/usr/bin/$$f; done || true
	# Dummy file to force OpenWRT to use custom postinst
endef

