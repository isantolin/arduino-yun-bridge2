include $(TOPDIR)/rules.mk

PKG_NAME:=python3-tzdata
PKG_VERSION:=2026.3
PKG_RELEASE:=1

PYPI_NAME:=tzdata
PKG_HASH:=4a1518b8993086a7982523e071643f3c0e5f213e75b21318e78bcabfff9d1415

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=Apache-2.0
PKG_LICENSE_FILES:=LICENSE

# PKG_BUILD_DEPENDS pre-injected into hostpkg

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-tzdata
	SECTION:=lang
	CATEGORY:=Languages
	SUBMENU:=Python
	TITLE:=IANA time zone data for Python
	URL:=https://github.com/python/tzdata
	DEPENDS:=+python3-light
endef

define Package/python3-tzdata/description
 Provides the IANA time zone database (tzdata) as a Python package,
 required by pendulum for timezone-aware datetime handling.
endef

$(eval $(call Py3Package,python3-tzdata))
$(eval $(call BuildPackage,python3-tzdata))
