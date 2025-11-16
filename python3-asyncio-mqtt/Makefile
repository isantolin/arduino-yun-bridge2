include $(TOPDIR)/rules.mk

PKG_NAME:=python-asyncio-mqtt
PKG_VERSION:=0.12.1
PKG_RELEASE:=1

PYPI_NAME:=asyncio-mqtt
PKG_SOURCE:=asyncio_mqtt-$(PKG_VERSION).tar.gz
PKG_SOURCE_URL:=https://files.pythonhosted.org/packages/source/a/$(PYPI_NAME)/
PKG_HASH:=6dbf85a45f94d26e7465411680ada3947b50546e98208d5d52e7a0a7ed7a7c38

PKG_LICENSE:=BSD-3-Clause
PKG_LICENSE_FILES:=README.md
PKG_MAINTAINER:=Ignacio Santolin <ignaciosantolin@example.com>

PKG_BUILD_DIR:=$(BUILD_DIR)/asyncio_mqtt-$(PKG_VERSION)
PYTHON3_PKG_WHEEL_NAME:=asyncio_mqtt

include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-asyncio-mqtt
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Asyncio wrapper around paho-mqtt
  URL:=https://github.com/sbtinstruments/asyncio-mqtt
  DEPENDS:=+python3-light +python3-asyncio +python3-paho-mqtt
endef

define Package/python3-asyncio-mqtt/description
 Idiomatic asyncio client built on top of paho-mqtt.
endef

$(eval $(call Py3Package,python3-asyncio-mqtt))
$(eval $(call BuildPackage,python3-asyncio-mqtt))
