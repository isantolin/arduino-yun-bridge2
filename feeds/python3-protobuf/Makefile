# Receta de OpenWrt para Google Protocol Buffers en Python 3
include $(TOPDIR)/rules.mk

PKG_NAME:=python3-protobuf
PKG_VERSION:=5.28.3
PKG_RELEASE:=1

PYPI_NAME:=protobuf
PKG_HASH:=64badbc49180a5e401f373f9ce7ab1d18b63f7dd4a9cdc43c92b9f0b481cef7b

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=BSD-3-Clause
PKG_LICENSE_FILES:=LICENSE

# PKG_BUILD_DEPENDS pre-injected into hostpkg

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

TARGET_CFLAGS += -I$(STAGING_DIR)/usr/include/python$(PYTHON3_VERSION)

define Package/python3-protobuf
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Protocol Buffers para Python 3
  URL:=https://developers.google.com/protocol-buffers
  DEPENDS:=+python3-light +python3-logging
endef

define Package/python3-protobuf/description
  Protocol Buffers es el mecanismo extensible, neutral en cuanto a plataforma y lenguaje, de Google para serializar datos estructurados.
endef

$(eval $(call Py3Package,python3-protobuf))
$(eval $(call BuildPackage,python3-protobuf))

