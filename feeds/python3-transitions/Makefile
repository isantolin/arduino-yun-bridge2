include $(TOPDIR)/rules.mk

PKG_NAME:=python3-transitions
PKG_VERSION:=0.9.3
PKG_RELEASE:=1

PYPI_NAME:=transitions
PKG_SOURCE:=$(PYPI_NAME)-$(PKG_VERSION).tar.gz
PKG_HASH:=881fb75bb1654ed55d86060bb067f2c716f8e155f57bb73fd444e53713aafec8

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-transitions
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Lightweight finite state machine implementation
  URL:=https://github.com/pytransitions/transitions
  DEPENDS:=+python3-light
endef

define Package/python3-transitions/description
  transitions is a lightweight, object-oriented finite state machine
  implementation for Python.
endef

$(eval $(call Py3Package,python3-transitions))
$(eval $(call BuildPackage,python3-transitions))
