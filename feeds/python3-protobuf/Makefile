# Receta de OpenWrt para Google Protocol Buffers en Python 3
include $(TOPDIR)/rules.mk

PKG_NAME:=python3-protobuf
PKG_VERSION:=7.36.0_rc1
PKG_RELEASE:=1

PYPI_NAME:=protobuf
PYTHON3_PKG_WHEEL_VERSION:=7.36.0rc1
PKG_SOURCE:=protobuf-7.36.0rc1.tar.gz
PKG_BUILD_DIR:=$(BUILD_DIR)/pypi/protobuf-7.36.0rc1
PKG_HASH:=a7cf7138eff7c0d1c456eea80bc5854d02ac36a76460c33b3ebec216b2024d53

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=BSD-3-Clause
PKG_LICENSE_FILES:=LICENSE

PKG_BUILD_DEPENDS:=python3-setuptools/host python-build/host python-installer/host python-wheel/host

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

