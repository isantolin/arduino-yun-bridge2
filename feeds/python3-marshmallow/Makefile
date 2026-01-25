include $(TOPDIR)/rules.mk

PKG_NAME:=python3-marshmallow
PKG_VERSION:=3.26.1
PKG_RELEASE:=1

PYPI_NAME:=marshmallow
PKG_HASH:=3350409f20a70a7e4e11a27661187b77cdcaeb20abca41c1454fe33636bea09c
PKG_SOURCE:=$(PYPI_NAME)-$(PKG_VERSION).tar.gz
PKG_SOURCE_URL:=https://files.pythonhosted.org/packages/source/m/marshmallow/

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE

PKG_BUILD_DIR:=$(BUILD_DIR)/$(PYPI_NAME)-$(PKG_VERSION)
PYTHON3_PKG_WHEEL_NAME:=$(PYPI_NAME)

include $(INCLUDE_DIR)/package.mk
include $(INCLUDE_DIR)/../feeds/packages/lang/python/python3-package.mk

define Package/python3-marshmallow
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=A simplified object-to-object mapping library
  URL:=https://github.com/marshmallow-code/marshmallow
  DEPENDS:=+python3
endef

define Package/python3-marshmallow/description
  marshmallow is an ORM/ODM/framework-agnostic library for converting
  complex data types, such as objects, to and from native Python datatypes.
endef

$(eval $(call Py3Package,python3-marshmallow))
$(eval $(call BuildPackage,python3-marshmallow))
