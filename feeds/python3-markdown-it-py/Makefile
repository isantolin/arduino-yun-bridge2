include $(TOPDIR)/rules.mk

PKG_NAME:=python3-markdown-it-py
PKG_VERSION:=4.0.0
PKG_RELEASE:=1

PYPI_NAME:=markdown-it-py
PKG_HASH:=87327c59b172c5011896038353a81343b6754500a08cd7a4973bb48c6d578147

PKG_BUILD_DEPENDS:=python-setuptools/host python-build/host python-installer/host python-wheel/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-markdown-it-py
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Markdown parser in Python
  URL:=https://github.com/executablebooks/markdown-it-py
  DEPENDS:=+python3 +python3-mdurl
endef

define Package/python3-markdown-it-py/description
  Markdown parser in Python.
endef

$(eval $(call Py3Package,python3-markdown-it-py))
$(eval $(call BuildPackage,python3-markdown-it-py))
