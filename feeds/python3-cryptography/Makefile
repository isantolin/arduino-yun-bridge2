#
# Copyright (C) 2025 Ignacio Santolin
#
# This is free software, licensed under the GNU General Public License v2.
# See /LICENSE for more information.
#

include $(TOPDIR)/rules.mk

PKG_NAME:=python3-cryptography
PKG_VERSION:=46.0.4
PKG_RELEASE:=1

PYPI_NAME:=cryptography
PKG_HASH:=skip

PKG_MAINTAINER:=Ignacio Santolin <ignacio@example.com>
PKG_LICENSE:=Apache-2.0 BSD-3-Clause
PKG_LICENSE_FILES:=LICENSE LICENSE.APACHE LICENSE.BSD

PKG_BUILD_DEPENDS:=python-cffi/host python-maturin/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-cryptography
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Python Cryptography Library
  URL:=https://github.com/pyca/cryptography
  DEPENDS:=+python3 +python3-cffi +libopenssl +librt
endef

define Package/python3-cryptography/description
  cryptography is a package which provides cryptographic recipes and primitives to Python developers.
endef

$(eval $(call Py3Package,python3-cryptography))
$(eval $(call BuildPackage,python3-cryptography))
