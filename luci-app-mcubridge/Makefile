
include $(TOPDIR)/rules.mk

PKG_NAME:=luci-app-mcubridge
PKG_VERSION:=1.0
PKG_RELEASE:=3

PKG_MAINTAINER:=Ignacio Santolin and contributors

MQTT_JS_VERSION:=5.14.1
MQTT_JS_HASH:=a72e7b7c2bad997f81c9b394d38bc9cdb79c49dfb59f9ec729030c7e6209ac1e
MQTT_JS_URL:=https://unpkg.com/mqtt@$(MQTT_JS_VERSION)/dist/
MQTT_JS_DL_FILE:=mqtt-$(MQTT_JS_VERSION).min.js

include $(INCLUDE_DIR)/package.mk

define Download/mqtt_js
	FILE:=$(MQTT_JS_DL_FILE)
	URL:=$(MQTT_JS_URL)
	URL_FILE:=mqtt.min.js
	HASH:=$(MQTT_JS_HASH)
endef

$(eval $(call Download,mqtt_js))

PKG_BUILD_DIR := $(BUILD_DIR)/$(PKG_NAME)-$(PKG_VERSION)

define Build/Compile
endef


define Package/$(PKG_NAME)
	SECTION:=luci
	CATEGORY:=LuCI
	SUBMENU:=3. Applications
	TITLE:=LuCI Support for McuBridge (system Python only)
	PKG_ARCH:=all
	DEPENDS:=+luci-base +luci-compat +luci-lua-runtime +uhttpd-mod-lua +openwrt-mcu-core +mosquitto-client +lua +luaposix
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
	# ESTA ES LA LÍNEA QUE FALTABA:
	$(CP) ./luasrc/view/mcubridge/mqtt_port_helper.htm $(1)/usr/lib/lua/luci/view/mcubridge/
	
	$(CP) -r ./root/etc/config/mcubridge $(1)/etc/config/
	[ -d ./root/www/mcubridge ] && $(CP) -r ./root/www/mcubridge $(1)/www/ || true
	$(INSTALL_DIR) $(1)/www/mcubridge
	$(INSTALL_DATA) $(DL_DIR)/$(MQTT_JS_DL_FILE) $(1)/www/mcubridge/mqtt.min.js
endef

$(eval $(call BuildPackage,$(PKG_NAME)))

