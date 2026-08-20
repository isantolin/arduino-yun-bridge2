include $(TOPDIR)/rules.mk

PKG_NAME:=python3-grpclib
PKG_VERSION:=0.4.9
PKG_RELEASE:=1

PYPI_NAME:=grpclib
PKG_HASH:=cc589c330fa81004c6400a52a566407574498cb5b055fa927013361e21466c46

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=Apache-2.0
PKG_LICENSE_FILES:=LICENSE

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-grpclib
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Pure-Python gRPC implementation for asyncio
  URL:=https://github.com/vmagamedov/grpclib
  DEPENDS:=+python3-light +python3-h2
endef

define Package/python3-grpclib/description
  Pure-Python gRPC implementation using asyncio and HTTP/2.
endef

$(eval $(call Py3Package,python3-grpclib))
$(eval $(call BuildPackage,python3-grpclib))
