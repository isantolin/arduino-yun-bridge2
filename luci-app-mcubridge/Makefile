
include $(TOPDIR)/rules.mk

PKG_NAME:=luci-app-mcubridge
PKG_VERSION:=2.8.5
PKG_RELEASE:=1

PKG_MAINTAINER:=Ignacio Santolin and contributors

include $(INCLUDE_DIR)/package.mk

PKG_BUILD_DIR := $(BUILD_DIR)/$(PKG_NAME)-$(PKG_VERSION)

define Build/Compile
endef


define Package/$(PKG_NAME)
	SECTION:=luci
	CATEGORY:=LuCI
	SUBMENU:=3. Applications
	TITLE:=LuCI Support for McuBridge (system Python only)
	PKG_ARCH:=all
	DEPENDS:=+luci-base +luci-compat +luci-lua-runtime +uhttpd-mod-lua +mcubridge +lua +luaposix
endef

define Package/$(PKG_NAME)/description
 Web UI and CGI integration for McuBridge services on OpenWrt.
endef

define Package/$(PKG_NAME)/config
endef



define Package/$(PKG_NAME)/install
	# LuCI controller, model, and view files to correct subdirs
	$(INSTALL_DIR) $(1)/usr/lib/lua/luci/controller
	$(INSTALL_DIR) $(1)/usr/lib/lua/luci/model/cbi
	$(INSTALL_DIR) $(1)/usr/lib/lua/luci/view/mcubridge
	$(INSTALL_DIR) $(1)/etc/config
	$(INSTALL_DIR) $(1)/www
	$(CP) ./luasrc/controller/mcubridge.lua $(1)/usr/lib/lua/luci/controller/
	$(CP) ./luasrc/model/cbi/mcubridge.lua $(1)/usr/lib/lua/luci/model/cbi/
	$(CP) ./luasrc/view/mcubridge/status.htm $(1)/usr/lib/lua/luci/view/mcubridge/
	$(CP) ./luasrc/view/mcubridge/webui.htm $(1)/usr/lib/lua/luci/view/mcubridge/
	$(CP) ./luasrc/view/mcubridge/credentials.htm $(1)/usr/lib/lua/luci/view/mcubridge/
	$(CP) ./luasrc/view/mcubridge/capabilities.htm $(1)/usr/lib/lua/luci/view/mcubridge/
	
	$(CP) -r ./root/etc/config/mcubridge $(1)/etc/config/
	[ -d ./root/www/mcubridge ] && $(CP) -r ./root/www/mcubridge $(1)/www/ || true
endef

$(eval $(call BuildPackage,$(PKG_NAME)))
