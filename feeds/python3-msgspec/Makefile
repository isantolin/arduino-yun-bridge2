include $(TOPDIR)/rules.mk

PKG_NAME:=python3-msgspec
PKG_VERSION:=0.20.0
PKG_RELEASE:=1

PYPI_NAME:=msgspec
PKG_HASH:=23a6ec2a3b5038c233b04740a545856a068bc5cb8db184ff493a58e08c994fbf
PKG_SOURCE:=$(PYPI_NAME)-$(PKG_VERSION).tar.gz
PKG_SOURCE_URL:=https://files.pythonhosted.org/packages/source/m/msgspec/

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE

PKG_BUILD_DIR:=$(BUILD_DIR)/$(PYPI_NAME)-$(PKG_VERSION)
PYTHON3_PKG_WHEEL_NAME:=$(PYPI_NAME)

include $(INCLUDE_DIR)/package.mk
include $(INCLUDE_DIR)/../feeds/packages/lang/python/python3-package.mk

define Package/python3-msgspec
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=A fast serialization and validation library
  URL:=https://github.com/jcrist/msgspec
  DEPENDS:=+python3
endef

define Package/python3-msgspec/description
  msgspec is a fast serialization and validation library, with support for
  JSON, MessagePack, YAML, and TOML. It is designed to be high performance
  and memory efficient.
endef

$(eval $(call Py3Package,python3-msgspec))
$(eval $(call BuildPackage,python3-msgspec))
