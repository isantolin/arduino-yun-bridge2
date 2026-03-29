include $(TOPDIR)/rules.mk

PKG_NAME:=python3-persist-queue
PKG_VERSION:=1.1.0
PKG_RELEASE:=1

PYPI_NAME:=persist-queue
PYPI_SOURCE_NAME:=persist_queue
PKG_HASH:=4c042a3d39c9af4fa24497da76697fb4fff7abe6af04bcf55f099e0c23bc2c67

PKG_LICENSE:=BSD-3-Clause
PKG_LICENSE_FILES:=LICENSE

PKG_BUILD_DEPENDS:=python-setuptools/host python-build/host python-installer/host python-wheel/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-persist-queue
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Disk-backed persistent queue library
  URL:=https://github.com/peter-wangxu/persist-queue
  DEPENDS:=+python3
endef

define Package/python3-persist-queue/description
  Thread-safe disk-backed persistent queue implementations for Python.
endef

$(eval $(call Py3Package,python3-persist-queue))
$(eval $(call BuildPackage,python3-persist-queue))
