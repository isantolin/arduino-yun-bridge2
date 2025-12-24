include $(TOPDIR)/rules.mk

PKG_NAME:=python3-paho-mqtt
PKG_VERSION:=2.1.0
PKG_RELEASE:=1

PYPI_NAME:=paho-mqtt
PYPI_SOURCE_NAME:=paho_mqtt
PKG_SOURCE:=paho_mqtt-$(PKG_VERSION).tar.gz
PKG_HASH:=12d6e7511d4137555a3f6ea167ae846af2c7357b10bc6fa4f7c3968fc1723834

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=EPL-2.0
PKG_LICENSE_FILES:=LICENSE.txt

PKG_BUILD_DIR:=$(BUILD_DIR)/pypi/$(PYPI_SOURCE_NAME)-$(PKG_VERSION)

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-paho-mqtt
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=MQTT Python client library (v2.x Custom)
  URL:=http://eclipse.org/paho
  # Dependencias en el target
  DEPENDS:=+python3-light +python3-uuid +python3-logging +python3-openssl
endef

# ESTA SECCIÓN ES LA CORRECCIÓN CLAVE:
# Define las herramientas necesarias para compilar el paquete en el host
define Py3Package/python3-paho-mqtt/install
	$(INSTALL_DIR) $(1)/usr/lib/python$(PYTHON3_VERSION)/site-packages
endef

# Requerimos hatchling para el proceso de build (host)
PYTHON3_PKG_BUILD_DEPENDS:=hatchling

define Package/python3-paho-mqtt/description
  This code provides a client class which enable applications to connect to an
  MQTT broker to publish messages, and to subscribe to topics and receive
  published messages.
  Supports MQTT v5.0 and Callback API v2.
endef

$(eval $(call Py3Package,python3-paho-mqtt))
$(eval $(call BuildPackage,python3-paho-mqtt))