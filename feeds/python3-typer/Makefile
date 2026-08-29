include $(TOPDIR)/rules.mk

PKG_NAME:=python3-typer
PKG_VERSION:=0.27.2
PKG_RELEASE:=1

PYPI_NAME:=typer
PKG_HASH:=269b7eb9d3c202ca84b4bc9618cb04ebb43d3d4d1e567e4c768607232c05f945

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE

# PKG_BUILD_DEPENDS pre-injected into hostpkg

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-typer
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Library for building CLI applications
  URL:=https://typer.tiangolo.com/
  DEPENDS:=+python3-light +python3-annotated-doc +python3-shellingham
endef

define Package/python3-typer/description
  Typer is a library for building CLI applications that users will love using and developers will love creating.
endef

$(eval $(call Py3Package,python3-typer))
$(eval $(call BuildPackage,python3-typer))
