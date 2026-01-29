include $(TOPDIR)/rules.mk

PKG_NAME:=python3-msgspec
PKG_VERSION:=0.20.0
PKG_RELEASE:=1

PYPI_NAME:=msgspec
PKG_HASH:=93f23528edc51d9f686808a361728e903d6f2be55c901d6f5c92e44c6d546bfc

include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-msgspec
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=High-performance JSON/MessagePack library
  URL:=https://github.com/jcrist/msgspec
  DEPENDS:=+python3-light
endef

define Package/python3-msgspec/description
  A fast and efficient JSON/MessagePack library with validation and schema support.
  Optimized for resource-constrained environments.
endef

$(eval $(call Py3Package,python3-msgspec))
$(eval $(call BuildPackage,python3-msgspec))