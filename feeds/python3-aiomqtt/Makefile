include $(TOPDIR)/rules.mk

PKG_NAME:=python3-aiomqtt
PKG_VERSION:=2.5.0
PKG_RELEASE:=2

PYPI_NAME:=aiomqtt
PKG_SOURCE:=aiomqtt-$(PKG_VERSION).tar.gz
PKG_HASH:=70e181c140a54ae736394efe2b9e865f665551a5417f6957456cc46010487b21

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
  TITLE:=Idiomatic asyncio MQTT client (v2.5.0)
  URL:=https://github.com/empicano/aiomqtt
  PKG_ARCH:=all
  DEPENDS:=+python3-light +python3-paho-mqtt
endef

define Package/python3-aiomqtt/description
  The idiomatic asyncio MQTT client (formerly asyncio-mqtt).
  Targeting Paho MQTT v2.x compatibility.
endef

$(eval $(call Py3Package,python3-aiomqtt))
$(eval $(call BuildPackage,python3-aiomqtt))
