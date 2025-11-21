include $(TOPDIR)/rules.mk

PKG_NAME:=python-aiomqtt
PKG_VERSION:=2.4.0
PKG_RELEASE:=1

PYPI_NAME:=aiomqtt
PYPI_SOURCE_NAME:=aiomqtt
PKG_HASH:=ab0f18fc5b7ffaa57451c407417d674db837b00a9c7d953cccd02be64f046c17

PKG_LICENSE:=BSD-3-Clause
PKG_LICENSE_FILES:=LICENSE
PKG_MAINTAINER:=Ignacio Santolin <ignaciosantolin@example.com>

PKG_BUILD_DEPENDS:=python-hatchling/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-aiomqtt
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Idiomatic asyncio MQTT client built on top of paho-mqtt
  URL:=https://github.com/sbtinstruments/aiomqtt
  DEPENDS:=+python3-light +python3-asyncio +python3-paho-mqtt +python3-typing-extensions +python3-hatchling
endef

define Package/python3-aiomqtt/description
 aiomqtt provides an asyncio native MQTT client with MQTT v5 support.
endef

$(eval $(call Py3Package,python3-aiomqtt))
$(eval $(call BuildPackage,python3-aiomqtt))
