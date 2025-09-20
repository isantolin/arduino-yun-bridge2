include $(TOPDIR)/rules.mk

PKG_NAME:=luci-app-yunbridge
PKG_VERSION:=1.0
PKG_RELEASE:=1

PKG_MAINTAINER:=Your Name <you@example.com>

include $(INCLUDE_DIR)/package.mk

define Package/$(PKG_NAME)
  SECTION:=luci
  CATEGORY:=LuCI
  SUBMENU:=3. Applications
  TITLE:=LuCI Support for YunBridge
  DEPENDS:=+luci +yunbridge
endef

define Package/$(PKG_NAME)/install
	$(CP) ./luasrc $(1)/usr/lib/lua/
	$(CP) ./root/etc/config/yunbridge $(1)/etc/config/
endef

$(eval $(call BuildPackage,$(PKG_NAME)))
