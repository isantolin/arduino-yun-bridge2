include $(TOPDIR)/rules.mk

PKG_NAME:=python3-h2
PKG_VERSION:=4.4.1
PKG_RELEASE:=1

PYPI_NAME:=h2
PKG_HASH:=4e866ffb1a869ae14dd9b5e6beb5c24a13da0495ad72b65925ded182521c1516

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE

# PKG_BUILD_DEPENDS pre-injected into hostpkg

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-h2
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=HTTP/2 State Machine based protocol implementation
  URL:=https://github.com/python-hyper/h2
  DEPENDS:=+python3-light +python3-hpack +python3-hyperframe
endef

define Package/python3-h2/description
  Pure-Python HTTP/2 state machine protocol implementation.
endef

$(eval $(call Py3Package,python3-h2))
$(eval $(call BuildPackage,python3-h2))
