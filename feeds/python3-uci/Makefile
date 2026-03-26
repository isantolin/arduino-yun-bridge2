#
# Copyright (C) 2025 Arduino MCU Bridge Ecosystem
#
# This is a custom Makefile for python3-uci optimized for Python 3.13+
# and OpenWrt 25.12.0 (APK).
#

include $(TOPDIR)/rules.mk

PKG_NAME:=python3-uci
PKG_VERSION:=0.10.3
PKG_RELEASE:=1

PYPI_NAME:=pyuci
PKG_HASH:=3b0f72ea81ed7a7999b2eb73f57a93019c0d359efaa9efb44fdf377fa09c1da6

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=LGPL-2.1-or-later
PKG_LICENSE_FILES:=LICENSE

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-uci
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Python bindings for the UCI library
  URL:=https://github.com/turris/pyuci
  DEPENDS:=+python3-light +libuci
endef

define Package/python3-uci/description
  Python 3 bindings for the OpenWrt Unified Configuration Interface (UCI) library.
  Optimized for asynchronous and SIL-2 applications.
endef

$(eval $(call Py3Package,python3-uci))
$(eval $(call BuildPackage,python3-uci))
$(eval $(call BuildPackage,python3-uci-src))
