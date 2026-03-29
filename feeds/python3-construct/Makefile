include $(TOPDIR)/rules.mk

PKG_NAME:=python3-construct
PKG_VERSION:=2.10.70
PKG_RELEASE:=1

PYPI_NAME:=construct
PKG_HASH:=4d2472f9684731e58cc9c56c463be63baa1447d674e0d66aeb5627b22f512c29

PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE

PKG_BUILD_DEPENDS:=python-setuptools/host python-build/host python-installer/host python-wheel/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-construct
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Declarative parser and builder for binary data
  URL:=https://construct.readthedocs.io/
  DEPENDS:=+python3
endef

define Package/python3-construct/description
  Declarative and symmetrical parser and builder for binary data structures.
endef

$(eval $(call Py3Package,python3-construct))
$(eval $(call BuildPackage,python3-construct))
