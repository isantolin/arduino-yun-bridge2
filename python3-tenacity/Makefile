include $(TOPDIR)/rules.mk

PKG_NAME:=python-tenacity
PKG_VERSION:=9.1.2
PKG_RELEASE:=1

PYPI_NAME:=tenacity
PKG_SOURCE:=$(PYPI_NAME)-$(PKG_VERSION).tar.gz
PKG_SOURCE_URL:=https://files.pythonhosted.org/packages/source/t/$(PYPI_NAME)/
PKG_HASH:=1169d376c297e7de388d18b4481760d478b0e99a777cad3a9c86e556f4b697cb

PKG_LICENSE:=Apache-2.0
PKG_LICENSE_FILES:=LICENSE
PKG_MAINTAINER:=Ignacio Santolin <ignaciosantolin@example.com>

PKG_BUILD_DIR:=$(BUILD_DIR)/$(PYPI_NAME)-$(PKG_VERSION)
PKG_BUILD_DEPENDS:=python-setuptools-scm/host

PYTHON3_PKG_WHEEL_NAME:=tenacity

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-tenacity
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Retrying library for Python
  URL:=https://github.com/jd/tenacity
  DEPENDS:=+python3-light
endef

define Package/python3-tenacity/description
 General-purpose retrying library for Python.
endef

$(eval $(call Py3Package,python3-tenacity))
$(eval $(call BuildPackage,python3-tenacity))
