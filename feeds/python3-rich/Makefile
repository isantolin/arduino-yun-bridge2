include $(TOPDIR)/rules.mk

PKG_NAME:=python3-rich
PKG_VERSION:=14.3.3
PKG_RELEASE:=1

PYPI_NAME:=rich
PKG_HASH:=b8daa0b9e4eef54dd8cf7c86c03713f53241884e814f4e2f5fb342fe520f639b

PKG_BUILD_DEPENDS:=python-setuptools/host python-build/host python-installer/host python-wheel/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-rich
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Beautiful terminal formatting
  URL:=https://github.com/Textualize/rich
  DEPENDS:=+python3 +python3-pygments +python3-markdown-it-py
endef

define Package/python3-rich/description
  Rich is a Python library for rich text and beautiful formatting in the terminal.
endef

$(eval $(call Py3Package,python3-rich))
$(eval $(call BuildPackage,python3-rich))
