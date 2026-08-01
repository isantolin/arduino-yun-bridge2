include $(TOPDIR)/rules.mk

PKG_NAME:=python3-lark
PKG_VERSION:=1.3.1
PKG_RELEASE:=1

PYPI_NAME:=lark
PKG_HASH:=b426a7a6d6d53189d318f2b6236ab5d6429eaf09259f1ca33eb716eed10d2905

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE

# PKG_BUILD_DEPENDS pre-injected into hostpkg

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-lark
	SECTION:=lang
	CATEGORY:=Languages
	SUBMENU:=Python
	TITLE:=A modern parsing library for Python
	URL:=https://github.com/lark-parser/lark
	DEPENDS:=+python3-light
endef

define Package/python3-lark/description
 Lark is a parsing toolkit for Python, built with a focus on ergonomics,
 performance and modularity. It is used by cel-python to parse the
 Common Expression Language (CEL) grammar for protovalidate.
endef

$(eval $(call Py3Package,python3-lark))
$(eval $(call BuildPackage,python3-lark))
