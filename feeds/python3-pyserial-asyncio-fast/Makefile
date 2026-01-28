include $(TOPDIR)/rules.mk

PKG_NAME:=python3-pyserial-asyncio-fast
PKG_VERSION:=0.16
PKG_RELEASE:=1

PYPI_NAME:=pyserial-asyncio-fast
PYPI_SOURCE_NAME:=pyserial_asyncio_fast
PKG_SOURCE:=$(PYPI_SOURCE_NAME)-$(PKG_VERSION).tar.gz
PKG_HASH:=fd52643380406739d777014b0aea0873d756b542eb62f7556567239cec007115

PKG_BUILD_DIR:=$(BUILD_DIR)/pypi/$(PYPI_SOURCE_NAME)-$(PKG_VERSION)

PYTHON3_PKG_WHEEL_NAME:=pyserial_asyncio_fast

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=BSD-3-Clause

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-pyserial-asyncio-fast
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Fast asyncio transport for pyserial
  URL:=https://github.com/vxgmichel/pyserial-asyncio-fast
  DEPENDS:=+python3-light +python3-pyserial
endef

define Package/python3-pyserial-asyncio-fast/description
  A fast asyncio transport for pyserial.
endef

$(eval $(call Py3Package,python3-pyserial-asyncio-fast))
$(eval $(call BuildPackage,python3-pyserial-asyncio-fast))
$(eval $(call BuildPackage,python3-pyserial-asyncio-fast-src))