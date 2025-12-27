include $(TOPDIR)/rules.mk

PKG_NAME:=python3-prometheus-client
PKG_VERSION:=0.21.1
PKG_RELEASE:=2

PYPI_NAME:=prometheus_client
PKG_HASH:=252505a722ac04b0456be05c05f75f45d760c2911ffc45f2a06bcaed9f3ae3fb

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=Apache-2.0

# Build system priority
include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

PKG_BUILD_DIR:=$(BUILD_DIR)/$(PYPI_NAME)-$(PKG_VERSION)

define Package/python3-prometheus-client
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Prometheus instrumentation library for Python
  URL:=https://github.com/prometheus/client_python
  # [OPTIMIZATION] Eliminado +python3-twisted (ahorra espacio y evita errores)
  DEPENDS:=+python3-light
endef

define Package/python3-prometheus-client/description
  The official Python client for Prometheus.
endef

$(eval $(call Py3Package,python3-prometheus-client))
$(eval $(call BuildPackage,python3-prometheus-client))
