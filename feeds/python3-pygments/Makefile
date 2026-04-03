include $(TOPDIR)/rules.mk

PKG_NAME:=python3-pygments
PKG_VERSION:=2.19.2
PKG_RELEASE:=1

PYPI_NAME:=pygments
PKG_HASH:=636cb2477cec7f8952536970bc533bc43743542f70392ae026374600add5b887

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
