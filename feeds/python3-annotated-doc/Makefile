include $(TOPDIR)/rules.mk

PKG_NAME:=python3-annotated-doc
PKG_VERSION:=0.0.5
PKG_RELEASE:=1

PYPI_NAME:=annotated-doc
PYTHON3_PKG_WHEEL_NAME:=annotated_doc
PKG_HASH:=c7e58ce09192557605d8bbd92836d7e1d520ac9580096042c0bfd197efacf1bb

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
