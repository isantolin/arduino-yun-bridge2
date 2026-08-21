include $(TOPDIR)/rules.mk

PKG_NAME:=python3-typer
PKG_VERSION:=0.27.1
PKG_RELEASE:=1

PYPI_NAME:=typer
PKG_HASH:=a79bef8469a79c45498e7b814ecf8d603cc7644e9acbd9e19cac0334240b18df

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
