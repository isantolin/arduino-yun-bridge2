include $(TOPDIR)/rules.mk

PKG_NAME:=python3-prometheus-client
PKG_VERSION:=0.21.1
PKG_RELEASE:=1

PYPI_NAME:=prometheus_client
PKG_HASH:=6ae8f9081eaaaf153a2e959d2e6c4f4fb57b12ef76c8c7980202f1e57b48b2ce

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=Apache-2.0

include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-prometheus-client
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Prometheus instrumentation library for Python
  URL:=https://github.com/prometheus/client_python
  DEPENDS:=+python3-light +python3-twisted
endef

define Package/python3-prometheus-client/description
  The official Python client for Prometheus.
endef

$(eval $(call Py3Package,python3-prometheus-client))
$(eval $(call BuildPackage,python3-prometheus-client))
