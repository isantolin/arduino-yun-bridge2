include $(TOPDIR)/rules.mk

PKG_NAME:=python3-cobs
PKG_VERSION:=1.2.2
PKG_RELEASE:=1

PYPI_NAME:=cobs
PKG_HASH:=dbdd5e32111d72786f83d0c269215dcd6ac629b1ac1962c6878221f3b2ca98da
PKG_SOURCE:=cobs-1.2.2.tar.gz
PKG_SOURCE_URL:=https://files.pythonhosted.org/packages/source/c/cobs/

PKG_BUILD_DIR:=$(BUILD_DIR)/pypi/cobs-1.2.2

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE

PKG_BUILD_DEPENDS:=python3-setuptools/host python-build/host python-installer/host python-wheel/host

# CORRECCIÓN: Forzar el nombre del wheel para que coincida con el output del build
PYTHON3_PKG_WHEEL_NAME:=$(PYPI_NAME)

include $(INCLUDE_DIR)/package.mk
include $(INCLUDE_DIR)/../feeds/packages/lang/python/python3-package.mk

define Package/python3-cobs
	SECTION:=lang
	CATEGORY:=Languages
	SUBMENU:=Python
	TITLE:=Consistent Overhead Byte Stuffing (COBS)
	URL:=https://github.com/cmcqueen/cobs-python
	DEPENDS:=+python3
endef

define Package/python3-cobs/description
 Consistent Overhead Byte Stuffing (COBS) encoding and decoding.
endef

$(eval $(call Py3Package,python3-cobs))
$(eval $(call BuildPackage,python3-cobs))
