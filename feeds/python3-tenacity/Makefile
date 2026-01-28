include $(TOPDIR)/rules.mk

PKG_NAME:=python3-tenacity
PKG_VERSION:=9.0.0
PKG_RELEASE:=1

PYPI_NAME:=tenacity
PKG_SOURCE:=$(PYPI_NAME)-$(PKG_VERSION).tar.gz
PKG_HASH:=807f37ca97d62aa361264d497b0e31e92b8027044942bfa756160d908320d73b

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=Apache-2.0
PKG_LICENSE_FILES:=LICENSE

PKG_BUILD_DEPENDS:=python-setuptools-scm/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-tenacity
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Retry library for Python
  URL:=https://github.com/jd/tenacity
  DEPENDS:=+python3-light
endef

define Package/python3-tenacity/description
  Tenacity is an Apache 2.0 licensed general-purpose retrying library,
  written in Python, to simplify the task of adding retry behavior to
  just about anything.
endef

$(eval $(call Py3Package,python3-tenacity))
$(eval $(call BuildPackage,python3-tenacity))
$(eval $(call BuildPackage,python3-tenacity-src))
