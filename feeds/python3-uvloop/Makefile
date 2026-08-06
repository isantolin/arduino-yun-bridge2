include $(TOPDIR)/rules.mk

PKG_NAME:=python3-uvloop
PKG_VERSION:=0.22.1
PKG_RELEASE:=3

PYPI_NAME:=uvloop
PKG_HASH:=6c84bae345b9147082b17371e3dd5d42775bddce91f885499017f4607fdaf39f

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE
# PKG_BUILD_DEPENDS pre-injected into hostpkg

# Force usage of system libuv (provided by DEPENDS:=+libuv)
PYTHON3_PKG_BUILD_VARS+=UVLOOP_USE_SYSTEM_LIBUV=1 UVLOOP_BUILD_LIBUV=0 LIBUV_CONFIGURE_HOST=$(GNU_TARGET_NAME)

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk

include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

TARGET_CFLAGS += -I$(STAGING_DIR)/usr/include -I$(STAGING_DIR)/usr/include/python$(PYTHON3_VERSION)
TARGET_LDFLAGS += -L$(STAGING_DIR)/usr/lib

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

define Build/Compile
	UVLOOP_USE_SYSTEM_LIBUV=1 $(call Py3Build/Compile)
endef

$(eval $(call Py3Package,python3-uvloop))
$(eval $(call BuildPackage,python3-uvloop))