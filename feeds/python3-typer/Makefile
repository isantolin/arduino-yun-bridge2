include $(TOPDIR)/rules.mk

PKG_NAME:=python3-typer
PKG_VERSION:=0.24.1
PKG_RELEASE:=1

PYPI_NAME:=typer
PKG_HASH:=e39b4732d65fbdcde189ae76cf7cd48aeae72919dea1fdfc16593be016256b45

# typer 0.24.1 needs pdm.backend which is injected into the SDK's host Python
# by 1_compile.sh after the host Python is built (before package compilation).
PKG_BUILD_DEPENDS:=python-build/host python-installer/host python-wheel/host

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
