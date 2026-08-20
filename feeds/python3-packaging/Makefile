include $(TOPDIR)/rules.mk

PKG_NAME:=python3-packaging
PKG_VERSION:=26.3
PKG_RELEASE:=1

PYPI_NAME:=packaging
PKG_HASH:=94edc256424af38762eb31306eed28beb9f0efc50a8837492c9d6fd6004aed79

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=Apache-2.0 OR BSD-2-Clause
PKG_LICENSE_FILES:=LICENSE LICENSE.APACHE LICENSE.BSD

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-packaging
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Core utilities for Python packages
  URL:=https://github.com/pypa/packaging
  DEPENDS:=+python3-light
endef

define Package/python3-packaging/description
  Core utilities for Python packages.
endef

$(eval $(call Py3Package,python3-packaging))
$(eval $(call BuildPackage,python3-packaging))
