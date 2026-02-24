include $(TOPDIR)/rules.mk

PKG_NAME:=python3-typer
PKG_VERSION:=0.15.1
PKG_RELEASE:=1

PYPI_NAME:=typer
PKG_HASH:=a0588c0a7fa68a1978a069818657778f86abe6ff5ea6abf472f940a08bfe4f0a

PKG_BUILD_DEPENDS:=python-setuptools/host python-build/host python-installer/host python-wheel/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-typer
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Library for building CLI applications
  URL:=https://typer.tiangolo.com/
  DEPENDS:=+python3 +python3-click +python3-shellingham
endef

define Package/python3-typer/description
  Typer is a library for building CLI applications that users will love using and developers will love creating.
endef

$(eval $(call Py3Package,python3-typer))
$(eval $(call BuildPackage,python3-typer))
