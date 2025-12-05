include $(TOPDIR)/rules.mk

PKG_NAME:=python-prometheus-client
PKG_VERSION:=0.23.1
PKG_RELEASE:=1

PYPI_NAME:=prometheus_client
PKG_HASH:=6ae8f9081eaaaf153a2e959d2e6c4f4fb57b12ef76c8c7980202f1e57b48b2ce

PKG_LICENSE:=Apache-2.0
PKG_LICENSE_FILES:=LICENSE
PKG_MAINTAINER:=Ignacio Santolin <ignaciosantolin@example.com>

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-prometheus-client
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Prometheus instrumentation client for Python
  URL:=https://github.com/prometheus/client_python
  DEPENDS:=+python3-light
endef

define Package/python3-prometheus-client/description
 Prometheus instrumentation utilities for Python applications.
endef

$(eval $(call Py3Package,python3-prometheus-client))
$(eval $(call BuildPackage,python3-prometheus-client))
