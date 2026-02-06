include $(TOPDIR)/rules.mk

PKG_NAME:=python3-uvloop
PKG_VERSION:=0.22.1
PKG_RELEASE:=1

PYPI_NAME:=uvloop
PKG_SOURCE:=uvloop-$(PKG_VERSION).tar.gz
PKG_HASH:=6c84bae345b9147082b17371e3dd5d42775bddce91f885499017f4607fdaf39f

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE
# PKG_BUILD_DEPENDS:=python3-cython/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-uvloop
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Ultra fast asyncio event loop (v0.22.1)
  URL:=https://github.com/MagicStack/uvloop
  DEPENDS:=+python3 +libuv
endef

define Package/python3-uvloop/description
  uvloop is a fast, drop-in replacement of the built-in asyncio event loop.
  uvloop is implemented in Cython and uses libuv under the hood.
endef

$(eval $(call Py3Package,python3-uvloop))
$(eval $(call BuildPackage,python3-uvloop))
