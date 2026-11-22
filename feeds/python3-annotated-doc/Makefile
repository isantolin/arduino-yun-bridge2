include $(TOPDIR)/rules.mk

PKG_NAME:=python3-annotated-doc
PKG_VERSION:=0.0.7
PKG_RELEASE:=1

PYPI_NAME:=annotated_doc
PKG_HASH:=0a30ef8f5322a4173720ae6712dc9a5e58f5a19868f3f167b4b5778db066c2b5

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-annotated-doc
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Doc docstrings extraction runtime
  URL:=https://github.com/tiangolo/annotated-doc
  DEPENDS:=+python3-light
endef

define Package/python3-annotated-doc/description
  Doc docstrings extraction runtime required by Typer.
endef

$(eval $(call Py3Package,python3-annotated-doc))
$(eval $(call BuildPackage,python3-annotated-doc))
