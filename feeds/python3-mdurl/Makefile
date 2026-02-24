include $(TOPDIR)/rules.mk

PKG_NAME:=python3-mdurl
PKG_VERSION:=0.1.2
PKG_RELEASE:=1

PYPI_NAME:=mdurl
PKG_HASH:=bb413d29f5eea38f31dd4754dd7377d4465116fb207585f97bf925588687c1ba

PKG_BUILD_DEPENDS:=python-setuptools/host python-build/host python-installer/host python-wheel/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-mdurl
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Markdown URL utilities
  URL:=https://github.com/executablebooks/mdurl
  DEPENDS:=+python3
endef

define Package/python3-mdurl/description
  Markdown URL utilities.
endef

$(eval $(call Py3Package,python3-mdurl))
$(eval $(call BuildPackage,python3-mdurl))
