include $(TOPDIR)/rules.mk

PKG_NAME:=openwrt-yun-core
PKG_VERSION:=1.0
PKG_RELEASE:=1

PKG_MAINTAINER:=Your Name <you@example.com>

include $(INCLUDE_DIR)/package.mk

define Package/$(PKG_NAME)
  SECTION:=base
  CATEGORY:=Base system
  TITLE:=YunBridge Core Files
endef

define Package/$(PKG_NAME)/install
	$(INSTALL_DIR) $(1)/etc/yunbridge
	$(INSTALL_DATA) ./yunbridge.files $(1)/etc/yunbridge/
	$(INSTALL_DIR) $(1)/etc/config
	$(INSTALL_CONF) ./99-yunbridge-ttyath0.conf $(1)/etc/config/yunbridge-ttyath0
	$(INSTALL_DIR) $(1)/etc/init.d
	$(INSTALL_BIN) ./yunbridge.init $(1)/etc/init.d/yunbridge
	$(INSTALL_DIR) $(1)/www/cgi-bin
	[ -f ../scripts/pin_rest_cgi.py ] && $(INSTALL_BIN) ../scripts/pin_rest_cgi.py $(1)/www/cgi-bin/pin || true
	$(INSTALL_DIR) $(1)/usr/bin
	[ -d ../scripts ] && for f in ../scripts/*; do $(INSTALL_BIN) $$f $(1)/usr/bin/; done || true
	$(INSTALL_DIR) $(1)/CONTROL
	$(INSTALL_BIN) ./postinst $(1)/CONTROL/postinst
endef

$(eval $(call BuildPackage,$(PKG_NAME)))
