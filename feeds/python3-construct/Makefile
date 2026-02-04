include $(TOPDIR)/rules.mk

PKG_NAME:=python3-construct
PKG_VERSION:=2.10.70
PKG_RELEASE:=1

PYPI_NAME:=construct
PKG_SOURCE:=construct-$(PKG_VERSION).tar.gz
PKG_HASH:=b48dbb92b80976453ee04e874e259afc19bed1956845c94c9275faee70149fa1

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-construct
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Declarative data structures for python (v2.10.70)
  URL:=https://construct.readthedocs.io/
  PKG_ARCH:=all
  DEPENDS:=+python3-light
endef

define Package/python3-construct/description
  Construct is a powerful declarative and symmetrical parser and builder for binary data.
endef

$(eval $(call Py3Package,python3-construct))
$(eval $(call BuildPackage,python3-construct))
