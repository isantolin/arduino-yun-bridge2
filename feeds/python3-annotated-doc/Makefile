include $(TOPDIR)/rules.mk

PKG_NAME:=python3-annotated-doc
PKG_VERSION:=0.0.6
PKG_RELEASE:=1

PYPI_NAME:=annotated_doc
PKG_HASH:=7969bb69912085b2285849c0dcac0b4b374fd180c13cf10a87f284ffcf347bbf

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
