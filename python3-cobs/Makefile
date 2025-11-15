include $(TOPDIR)/rules.mk

PKG_NAME:=python-cobs
PKG_VERSION:=1.2.2
PKG_RELEASE:=1

PYPI_NAME:=cobs
PKG_SOURCE:=$(PYPI_NAME)-$(PKG_VERSION).tar.gz
PKG_SOURCE_URL:=https://files.pythonhosted.org/packages/source/c/$(PYPI_NAME)/
PKG_HASH:=dbdd5e32111d72786f83d0c269215dcd6ac629b1ac1962c6878221f3b2ca98da

PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE
PKG_MAINTAINER:=Ignacio Santolin <ignaciosantolin@example.com>

PKG_BUILD_DIR:=$(BUILD_DIR)/$(PYPI_NAME)-$(PKG_VERSION)
PYTHON3_PKG_WHEEL_NAME:=cobs

include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-cobs
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Consistent Overhead Byte Stuffing encoder/decoder
  URL:=https://pypi.org/project/cobs/
  DEPENDS:=+python3-light
endef

define Package/python3-cobs/description
 COBS encoder/decoder implementation for Python with optional C extension.
endef

$(eval $(call Py3Package,python3-cobs))
$(eval $(call BuildPackage,python3-cobs))
$(eval $(call BuildPackage,python3-cobs-src))
