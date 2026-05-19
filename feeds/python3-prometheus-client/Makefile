include $(TOPDIR)/rules.mk

PKG_NAME:=python3-prometheus-client
PKG_VERSION:=0.25.0
PKG_RELEASE:=1

PYPI_NAME:=prometheus_client
PKG_HASH:=5e373b75c31afb3c86f1a52fa1ad470c9aace18082d39ec0d2f918d11cc9ba28

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=Apache-2.0

# Build system priority
include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

# [FIX] Agregar '/pypi/' a la ruta de construcción para cumplir con pypi.mk
PKG_BUILD_DIR:=$(BUILD_DIR)/pypi/$(PYPI_NAME)-$(PKG_VERSION)

define Package/python3-prometheus-client
	SECTION:=lang
	CATEGORY:=Languages
	SUBMENU:=Python
	TITLE:=Prometheus instrumentation library for Python
	URL:=https://github.com/prometheus/client_python
	PKG_ARCH:=all
  # [OPTIMIZATION] Eliminado +python3-twisted
	DEPENDS:=+python3-light
endef

define Package/python3-prometheus-client/description
 The official Python client for Prometheus.
endef

$(eval $(call Py3Package,python3-prometheus-client))
$(eval $(call BuildPackage,python3-prometheus-client))
