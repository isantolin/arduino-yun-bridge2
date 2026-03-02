include $(TOPDIR)/rules.mk

PKG_NAME:=python3-sh
PKG_VERSION:=2.2.2
PKG_RELEASE:=1

PYPI_NAME:=sh
PKG_SOURCE:=$(PYPI_NAME)-$(PKG_VERSION).tar.gz
PKG_HASH:=653227a7c41a284ec5302173fbc044ee817c7bad5e6e4d8d55741b9aeb9eb65b

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE.txt

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-sh
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Python subprocess interface
  URL:=https://github.com/amoffat/sh
  DEPENDS:=+python3-light
endef

define Package/python3-sh/description
  sh is a full-fledged subprocess replacement for Python that allows
  you to call any program as if it were a function.
endef

$(eval $(call Py3Package,python3-sh))
$(eval $(call BuildPackage,python3-sh))
