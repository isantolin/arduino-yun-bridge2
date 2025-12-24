include $(TOPDIR)/rules.mk

PKG_NAME:=python3-paho-mqtt
PKG_VERSION:=2.1.0
PKG_RELEASE:=1

PYPI_NAME:=paho-mqtt
PKG_SOURCE:=paho_mqtt-$(PKG_VERSION).tar.gz
PKG_HASH:=3602ce9d1ada58b063f2052ff013ff8cdd06d66cef918d5f6d048b6f68ccf049

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=EPL-2.0
PKG_LICENSE_FILES:=LICENSE.txt

include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-paho-mqtt
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=MQTT Python client library (v2.x Custom)
  URL:=http://eclipse.org/paho
  DEPENDS:=+python3-light +python3-uuid
  PROVIDES:=python3-paho-mqtt
endef

define Package/python3-paho-mqtt/description
  This code provides a client class which enable applications to connect to an
  MQTT broker to publish messages, and to subscribe to topics and receive
  published messages. Supports MQTT v5.0 and Callback API v2.
endef

$(eval $(call Py3Package,python3-paho-mqtt))
$(eval $(call BuildPackage,python3-paho-mqtt))
