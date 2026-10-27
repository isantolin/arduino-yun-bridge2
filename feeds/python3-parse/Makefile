include $(TOPDIR)/rules.mk

PKG_NAME:=python3-parse
PKG_VERSION:=1.22.4
PKG_RELEASE:=1

PYPI_NAME:=parse
PKG_HASH:=706d458feed5a81b02768d3156e8070baf6cee7d150a3a3c0cd43101a875c3b5

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-parse
	SECTION:=lang
	CATEGORY:=Languages
	SUBMENU:=Python
	TITLE:=parse() is the opposite of format()
	URL:=https://github.com/r1chardj0n3s/parse
	DEPENDS:=+python3-light
endef

define Package/python3-parse/description
 parse() is the opposite of format(). It provides declarative pattern-based string parsing.
endef

$(eval $(call Py3Package,python3-parse))
$(eval $(call BuildPackage,python3-parse))
