include $(TOPDIR)/rules.mk

PKG_NAME:=python3-aiomqtt
PKG_VERSION:=2.4.0
PKG_RELEASE:=1

PYPI_NAME:=aiomqtt
PKG_HASH:=ab0f18fc5b7ffaa57451c407417d674db837b00a9c7d953cccd02be64f046c17

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=BSD-3-Clause
PKG_LICENSE_FILES:=LICENSE

include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-aiomqtt
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Idiomatic asyncio MQTT client (v2.4.0)
  URL:=https://github.com/empicano/aiomqtt
  DEPENDS:=+python3-light +python3-paho-mqtt
endef

define Package/python3-aiomqtt/description
  The idiomatic asyncio MQTT client (formerly asyncio-mqtt).
  Targeting Paho MQTT v2.x compatibility.
endef

$(eval $(call Py3Package,python3-aiomqtt))
$(eval $(call BuildPackage,python3-aiomqtt))
