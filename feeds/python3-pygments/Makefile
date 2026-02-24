include $(TOPDIR)/rules.mk

PKG_NAME:=python3-pygments
PKG_VERSION:=2.19.1
PKG_RELEASE:=1

PYPI_NAME:=pygments
PKG_HASH:=61c16d2a8576dc0649d9f39e089b5f02bcd27fba10d8fb4dcc28173f7a45151f

PKG_BUILD_DEPENDS:=python-setuptools/host python-build/host python-installer/host python-wheel/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-pygments
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Generic syntax highlighter
  URL:=https://pygments.org/
  DEPENDS:=+python3
endef

define Package/python3-pygments/description
  Pygments is a generic syntax highlighter suitable for use in code hosting,
  forums, wikis or other applications that need to prettify source code.
endef

$(eval $(call Py3Package,python3-pygments))
$(eval $(call BuildPackage,python3-pygments))
