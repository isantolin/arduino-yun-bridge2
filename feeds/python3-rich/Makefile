include $(TOPDIR)/rules.mk

PKG_NAME:=python3-rich
PKG_VERSION:=13.9.4
PKG_RELEASE:=1

PYPI_NAME:=rich
PKG_HASH:=439594978a49a09530cff7ebc4b5c7103ef57baf48d5ea3184f21d9a2befa098

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
