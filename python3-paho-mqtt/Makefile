include $(TOPDIR)/rules.mk

PKG_NAME:=python-paho-mqtt
PKG_VERSION:=2.1.0
PKG_RELEASE:=1

PYPI_NAME:=paho-mqtt
PYPI_SOURCE_NAME:=paho-mqtt
PKG_SOURCE:=paho_mqtt-$(PKG_VERSION).tar.gz
PKG_SOURCE_URL:=https://files.pythonhosted.org/packages/source/p/$(PYPI_NAME)/
PKG_HASH:=12d6e7511d4137555a3f6ea167ae846af2c7357b10bc6fa4f7c3968fc1723834

PKG_LICENSE:=EPL-2.0 OR BSD-3-Clause
PKG_LICENSE_FILES:=LICENSE.txt
PKG_MAINTAINER:=Ignacio Santolin <ignaciosantolin@example.com>

PKG_BUILD_DIR:=$(BUILD_DIR)/paho_mqtt-$(PKG_VERSION)

include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-paho-mqtt
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Eclipse Paho MQTT client library
  URL:=https://github.com/eclipse/paho.mqtt.python
  DEPENDS:=+python3-light
endef

define Package/python3-paho-mqtt/description
 Eclipse Paho MQTT client supporting MQTT v5.0 and 3.1.1.
endef

$(eval $(call Py3Package,python3-paho-mqtt))
$(eval $(call BuildPackage,python3-paho-mqtt))
