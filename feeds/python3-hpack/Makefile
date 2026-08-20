include $(TOPDIR)/rules.mk

PKG_NAME:=python3-hpack
PKG_VERSION:=4.2.0
PKG_RELEASE:=1

PYPI_NAME:=hpack
PKG_HASH:=0895cfa3b5531fc65fe439c05eb65144f123bf7a394fcaa56aa423548d8e45c0

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-hpack
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Pure-Python HTTP/2 header encoding (HPACK) logic
  URL:=https://github.com/python-hyper/hpack
  DEPENDS:=+python3-light
endef

define Package/python3-hpack/description
  Pure-Python HTTP/2 header encoding (HPACK) logic.
endef

$(eval $(call Py3Package,python3-hpack))
$(eval $(call BuildPackage,python3-hpack))
