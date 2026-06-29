include $(TOPDIR)/rules.mk

PKG_NAME:=python3-aiomqtt
PKG_VERSION:=3.0.0a1
PKG_RELEASE:=1

PYPI_NAME:=aiomqtt
PKG_SOURCE:=aiomqtt-$(PKG_VERSION).tar.gz
PKG_HASH:=6499d41b9a933a7a42a1ccbee3baa7e2ccf1e11848f42513f8c0cea4ddff93fe

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=BSD-3-Clause
PKG_LICENSE_FILES:=LICENSE

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-aiomqtt
	SECTION:=lang
	CATEGORY:=Languages
	SUBMENU:=Python
	TITLE:=Idiomatic asyncio MQTT client
	URL:=https://github.com/empicano/aiomqtt
	PKG_ARCH:=all
	DEPENDS:=+python3-light +python3-mqtt5
endef

define Package/python3-aiomqtt/description
 The idiomatic asyncio MQTT client (formerly asyncio-mqtt).
 Idiomatic asyncio MQTT client (v3.0.0a1)
endef

$(eval $(call Py3Package,python3-aiomqtt))
$(eval $(call BuildPackage,python3-aiomqtt))
