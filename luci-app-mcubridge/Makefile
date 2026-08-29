include $(TOPDIR)/rules.mk

PKG_NAME:=luci-app-mcubridge
PKG_VERSION:=2.8.6
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
	TITLE:=LuCI Support for McuBridge (Modern LuCI JS)
	PKG_ARCH:=all
	DEPENDS:=+luci-base +mcubridge
endef

define Package/$(PKG_NAME)/description
 Web UI and LuCI JS integration for McuBridge services on OpenWrt.
endef

define Package/$(PKG_NAME)/install
	$(INSTALL_DIR) $(1)/usr/share/luci/menu.d
	$(INSTALL_DIR) $(1)/usr/share/rpcd/acl.d
	$(INSTALL_DIR) $(1)/www/luci-static/resources/view/mcubridge
	$(INSTALL_DIR) $(1)/etc/config
	$(INSTALL_DIR) $(1)/www
	$(CP) ./root/usr/share/luci/menu.d/* $(1)/usr/share/luci/menu.d/
	$(CP) ./root/usr/share/rpcd/acl.d/* $(1)/usr/share/rpcd/acl.d/
	$(CP) ./htdocs/luci-static/resources/view/mcubridge/* $(1)/www/luci-static/resources/view/mcubridge/
	$(CP) -r ./root/etc/config/mcubridge $(1)/etc/config/
	[ -d ./root/www/mcubridge ] && $(CP) -r ./root/www/mcubridge $(1)/www/ || true
	[ -d ./root/usr/share/ucode ] && $(INSTALL_DIR) $(1)/usr/share/ucode && $(CP) -r ./root/usr/share/ucode/* $(1)/usr/share/ucode/ || true
endef

$(eval $(call BuildPackage,$(PKG_NAME)))
