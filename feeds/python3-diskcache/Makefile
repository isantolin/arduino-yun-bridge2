include $(TOPDIR)/rules.mk

PKG_NAME:=python3-diskcache
PKG_VERSION:=5.6.3
PKG_RELEASE:=1

PYPI_NAME:=diskcache
PYPI_SOURCE_NAME:=diskcache
PKG_HASH:=2c3a3fa2743d8f2f252a265f17f596fca0e69d6ecb4a584f70b12ad84d6bd0dd

PKG_LICENSE:=Apache-2.0
PKG_LICENSE_FILES:=LICENSE

PKG_BUILD_DEPENDS:=python-setuptools/host python-build/host python-installer/host python-wheel/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-diskcache
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Disk and file backed cache library
  URL:=https://github.com/grantjenks/python-diskcache
  DEPENDS:=+python3
endef

define Package/python3-diskcache/description
  Disk Cache -- Disk and file backed persistent cache.
endef

$(eval $(call Py3Package,python3-diskcache))
$(eval $(call BuildPackage,python3-diskcache))
