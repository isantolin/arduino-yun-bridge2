
include $(TOPDIR)/rules.mk

PKG_NAME:=python3-pyserial-asyncio
PKG_VERSION:=0.6
PKG_RELEASE:=1

PKG_MAINTAINER:=Ignacio Santolin and contributors

include $(INCLUDE_DIR)/package.mk

define Package/$(PKG_NAME)
  SECTION:=lang
  CATEGORY:=Python
  SUBMENU:=Packages
  TITLE:=Python3 Async I/O extension for pySerial
  DEPENDS:=+python3 +python3-pyserial
endef

define Package/$(PKG_NAME)/description
  Async I/O extension package for the Python Serial Port Extension for OSX, Linux, BSD.
  It depends on pySerial and is compatible with Python 3.5 and later.
endef

define Build/Prepare
	mkdir -p $(PKG_BUILD_DIR)
	$(CP) -r ./serial_asyncio $(PKG_BUILD_DIR)/
endef

define Package/$(PKG_NAME)/install
	$(INSTALL_DIR) $(1)/usr/lib/python$(PYTHON3_VERSION)/site-packages
	$(CP) -r $(PKG_BUILD_DIR)/serial_asyncio $(1)/usr/lib/python$(PYTHON3_VERSION)/site-packages/
endef

$(eval $(call BuildPackage,$(PKG_NAME)))
