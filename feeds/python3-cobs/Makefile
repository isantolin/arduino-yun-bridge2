include $(TOPDIR)/rules.mk

PKG_NAME:=python3-cobs
PKG_VERSION:=1.2.1
PKG_RELEASE:=1

PYPI_NAME:=cobs
PKG_HASH:=0000000000000000000000000000000000000000000000000000000000000000 # Placeholder
PKG_SOURCE:=$(PYPI_NAME)-$(PKG_VERSION).tar.gz
PKG_SOURCE_URL:=https://files.pythonhosted.org/packages/source/c/cobs/

PKG_MAINTAINER:=Ignacio Santolin <ignacio@example.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE

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
