# Receta de OpenWrt para Google Protocol Buffers en Python 3
include $(TOPDIR)/rules.mk

PKG_NAME:=python3-protobuf
PKG_VERSION:=7.35.1
PKG_RELEASE:=1

PYPI_NAME:=protobuf
PKG_SOURCE:=$(PYPI_NAME)-$(PKG_VERSION).tar.gz
PKG_HASH:=ce115a26fe0c39a2c29973d914d327e516a6455464489fe3cd1e51a1b354f81a

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=BSD-3-Clause
PKG_LICENSE_FILES:=LICENSE

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

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
