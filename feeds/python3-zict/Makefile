include $(TOPDIR)/rules.mk

PKG_NAME:=python3-zict
PKG_VERSION:=3.0.0
PKG_RELEASE:=1

PYPI_NAME:=zict
PKG_SOURCE:=$(PYPI_NAME)-$(PKG_VERSION).tar.gz
PKG_HASH:=e321e263b6a97aafc0790c3cfb3c04656b7066e6738c37fffcca95d803c9fba5

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=BSD-3-Clause
PKG_LICENSE_FILES:=LICENSE.txt

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-zict
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Mutable mapping tools
  URL:=https://github.com/dask/zict
  DEPENDS:=+python3-light +python3-heapdict
endef

define Package/python3-zict/description
  Zict provides a set of MutableMapping classes that allow you to
  compose and transform mappings.
endef

$(eval $(call Py3Package,python3-zict))
$(eval $(call BuildPackage,python3-zict))
