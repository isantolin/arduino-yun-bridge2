include $(TOPDIR)/rules.mk

PKG_NAME:=python-pyserial-asyncio
PKG_VERSION:=0.6
PKG_RELEASE:=1

PYPI_NAME:=pyserial-asyncio
PKG_SOURCE:=$(PYPI_NAME)-$(PKG_VERSION).tar.gz
PKG_SOURCE_URL:=https://files.pythonhosted.org/packages/source/p/$(PYPI_NAME)/
PKG_HASH:=b6032923e05e9d75ec17a5af9a98429c46d2839adfaf80604d52e0faacd7a32f

PKG_LICENSE:=BSD-3-Clause
PKG_LICENSE_FILES:=LICENSE.txt
PKG_MAINTAINER:=Ignacio Santolin <ignaciosantolin@example.com>

PKG_BUILD_DIR:=$(BUILD_DIR)/$(PYPI_NAME)-$(PKG_VERSION)

include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-pyserial-asyncio
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Asyncio support for pyserial
  URL:=https://github.com/pyserial/pyserial-asyncio
  DEPENDS:=+python3-light +python3-pyserial
endef

define Package/python3-pyserial-asyncio/description
 Asyncio transport/protocol implementation for serial ports built on top of pyserial.
endef

$(eval $(call Py3Package,python3-pyserial-asyncio))
$(eval $(call BuildPackage,python3-pyserial-asyncio))
$(eval $(call BuildPackage,python3-pyserial-asyncio-src))
