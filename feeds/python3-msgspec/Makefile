include $(TOPDIR)/rules.mk

PKG_NAME:=python3-msgspec
PKG_VERSION:=0.20.0
PKG_RELEASE:=1

PYPI_NAME:=msgspec
PKG_HASH:=692349e588fde322875f8d3025ac01689fead5901e7fb18d6870a44519d62a29

PKG_BUILD_DEPENDS:=python-setuptools/host python-build/host python-installer/host python-wheel/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-msgspec
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=High-performance JSON/MessagePack library
  URL:=https://github.com/jcrist/msgspec
  DEPENDS:=+python3
endef

define Package/python3-msgspec/description
  A fast and efficient JSON/MessagePack library with validation and schema support.
  Optimized for resource-constrained environments.
endef

$(eval $(call Py3Package,python3-msgspec))
$(eval $(call BuildPackage,python3-msgspec))