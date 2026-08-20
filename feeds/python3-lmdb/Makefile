include $(TOPDIR)/rules.mk

PKG_NAME:=python3-lmdb
PKG_VERSION:=2.3.0
PKG_RELEASE:=1

PYPI_NAME:=lmdb
PKG_HASH:=260f443640ee2da3cfd059a84258659319ff39ca912c16bf9748c324076b9d09

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=OLDAP-2.8
PKG_LICENSE_FILES:=LICENSE

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-lmdb
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Universal Python binding for the LMDB Lightning Database
  URL:=https://github.com/jnwatson/py-lmdb
  DEPENDS:=+python3-light
endef

define Package/python3-lmdb/description
  Universal Python binding for the LMDB Lightning Database.
endef

$(eval $(call Py3Package,python3-lmdb))
$(eval $(call BuildPackage,python3-lmdb))
