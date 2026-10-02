include $(TOPDIR)/rules.mk

PKG_NAME:=python3-lmdb
PKG_VERSION:=3.0.0
PKG_RELEASE:=1

PYPI_NAME:=lmdb
PKG_HASH:=06dda0723545e14d56ac7dcd6f582d9922c2ed5e16f1c3f8d0e670a96ec2ee65

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=OLDAP-2.8
PKG_LICENSE_FILES:=LICENSE

PKG_BUILD_DEPENDS:=python3-setuptools/host python3-cffi/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Build/Prepare
	$(call Build/Prepare/Default)
	sed -i 's/, "patch-ng"//g' $(PKG_BUILD_DIR)/pyproject.toml || true
	sed -i 's/"patch-ng", //g' $(PKG_BUILD_DIR)/pyproject.toml || true
	sed -i 's/"patch-ng"//g' $(PKG_BUILD_DIR)/pyproject.toml || true
endef

define Package/python3-lmdb
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Universal Python binding for the LMDB Lightning Database
  URL:=https://github.com/jnwatson/py-lmdb
  DEPENDS:=+python3-light
endef

define Package/python3-lmdb/description
  Universal Python binding for the LMDB Lightning Database.
endef

$(eval $(call Py3Package,python3-lmdb))
$(eval $(call BuildPackage,python3-lmdb))
