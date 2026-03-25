include $(TOPDIR)/rules.mk

PKG_NAME:=python3-click
PKG_VERSION:=8.3.1
PKG_RELEASE:=1

PYPI_NAME:=click
PKG_HASH:=12ff4785d337a1bb490bb7e9c2b1ee5da3112e94a8622f26a6c77f5d2fc6842a

PKG_BUILD_DEPENDS:=python-setuptools/host python-build/host python-installer/host python-wheel/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-click
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Composable command line interface toolkit
  URL:=https://palletsprojects.com/p/click/
  DEPENDS:=+python3
endef

define Package/python3-click/description
  Click is a Python package for creating beautiful command line interfaces
  in a composable way with as little code as possible.
endef

$(eval $(call Py3Package,python3-click))
$(eval $(call BuildPackage,python3-click))
