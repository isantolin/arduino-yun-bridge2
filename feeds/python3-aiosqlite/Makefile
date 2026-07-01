# Receta de OpenWrt para aiosqlite en Python 3
include $(TOPDIR)/rules.mk

PKG_NAME:=python3-aiosqlite
PKG_VERSION:=0.22.1
PKG_RELEASE:=1

PYPI_NAME:=aiosqlite
PKG_SOURCE:=$(PYPI_NAME)-$(PKG_VERSION).tar.gz
PKG_HASH:=043e0bd78d32888c0a9ca90fc788b38796843360c855a7262a532813133a0650

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-aiosqlite
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Puente asyncio a sqlite3 para Python 3
  URL:=https://github.com/omnilib/aiosqlite
  DEPENDS:=+python3-light +python3-sqlite3
endef

define Package/python3-aiosqlite/description
  aiosqlite proporciona una interfaz asíncrona amigable para bases de datos sqlite3 en Python 3 usando asyncio.
endef

$(eval $(call Py3Package,python3-aiosqlite))
$(eval $(call BuildPackage,python3-aiosqlite))
