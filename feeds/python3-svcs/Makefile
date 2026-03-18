include $(TOPDIR)/rules.mk

PKG_NAME:=python3-svcs
PKG_VERSION:=25.1.0
PKG_RELEASE:=1

PYPI_NAME:=svcs
PKG_SOURCE:=$(PYPI_NAME)-$(PKG_VERSION).tar.gz
PKG_HASH:=64dd74d0c4e8fee79a9ac7550a9d824670b228df390ba1911614e29b59b3f2c2

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE

PKG_BUILD_DEPENDS:=python-hatch-vcs/host python-hatchling/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-svcs
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=A Structured Service Locator for Python
  URL:=https://github.com/hynek/svcs
  DEPENDS:=+python3-light +python3-attrs
endef

define Package/python3-svcs/description
  svcs is a dependency container for Python that gives you a
  structured way to acquire your services, with health checks
  and graceful cleanups.
endef

$(eval $(call Py3Package,python3-svcs))
$(eval $(call BuildPackage,python3-svcs))
