include $(TOPDIR)/rules.mk

PKG_NAME:=python-annotated-types
PKG_VERSION:=0.7.0
PKG_RELEASE:=1

PYPI_NAME:=annotated_types
PKG_HASH:=aff07c09a53a08bc8cfccb9c85b05f1aa9a2a6f23728d790723543408344ce89

PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE
PKG_MAINTAINER:=Ignacio Santolin <ignaciosantolin@example.com>

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-annotated-types
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Runtime utilities for typing.Annotated metadata
  URL:=https://pypi.org/project/annotated-types/
  DEPENDS:=+python3-light
endef

define Package/python3-annotated-types/description
 Helper functions that make it easier to work with typing.Annotated metadata.
endef

$(eval $(call Py3Package,python3-annotated-types))
$(eval $(call BuildPackage,python3-annotated-types))
