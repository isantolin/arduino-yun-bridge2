# Receta de OpenWrt para Google Protocol Buffers en Python 3
include $(TOPDIR)/rules.mk

PKG_NAME:=python3-protobuf
PKG_VERSION:=7.36.0_rc2
PKG_RELEASE:=1

PYPI_NAME:=protobuf
PKG_SOURCE:=protobuf-7.36.0rc2.tar.gz
PYTHON3_PKG_WHEEL_VERSION:=7.36.0rc2
PKG_HASH:=af5194b5cd9370d0a4f849c356ae05a0482ecc942570ae12bb37539b8c68c066

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=BSD-3-Clause
PKG_LICENSE_FILES:=LICENSE

# PKG_BUILD_DEPENDS pre-injected into hostpkg

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
PKG_BUILD_DIR:=$(BUILD_DIR)/pypi/protobuf-7.36.0rc2
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

