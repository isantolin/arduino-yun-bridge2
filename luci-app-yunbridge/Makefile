
include $(TOPDIR)/rules.mk

PKG_NAME:=luci-app-yunbridge
PKG_VERSION:=1.0
PKG_RELEASE:=1

PKG_MAINTAINER:=Your Name <you@example.com>

include $(INCLUDE_DIR)/package.mk

PKG_BUILD_DIR := $(BUILD_DIR)/$(PKG_NAME)-$(PKG_VERSION)

define Build/Compile
endef


define Package/$(PKG_NAME)
  SECTION:=luci
  CATEGORY:=LuCI
  SUBMENU:=3. Applications
	TITLE:=LuCI Support for YunBridge (system Python only)
		DEPENDS:=+luci-base +openwrt-yun-core
endef



define Package/$(PKG_NAME)/install
	# LuCI controller, model, and view files to correct subdirs
	$(INSTALL_DIR) $(1)/usr/lib/lua/luci/controller
	$(INSTALL_DIR) $(1)/usr/lib/lua/luci/model/cbi
	$(INSTALL_DIR) $(1)/usr/lib/lua/luci/view/yunbridge
	$(INSTALL_DIR) $(1)/etc/config
	$(INSTALL_DIR) $(1)/www
	$(CP) ./luasrc/controller/yunbridge.lua $(1)/usr/lib/lua/luci/controller/
	$(CP) ./luasrc/model/cbi/yunbridge.lua $(1)/usr/lib/lua/luci/model/cbi/
	$(CP) ./luasrc/view/yunbridge/status.htm $(1)/usr/lib/lua/luci/view/yunbridge/
	$(CP) ./luasrc/view/yunbridge/webui.htm $(1)/usr/lib/lua/luci/view/yunbridge/
	$(CP) -r ./root/etc/config/yunbridge $(1)/etc/config/
	[ -d ./root/www/yunbridge ] && $(CP) -r ./root/www/yunbridge $(1)/www/ || true
endef

$(eval $(call BuildPackage,$(PKG_NAME)))

