include $(TOPDIR)/rules.mk

PKG_NAME:=python3-cel-python
PKG_VERSION:=0.5.0
PKG_RELEASE:=1

PYPI_NAME:=cel_python
PKG_HASH:=3eb0a619e8df0f338d0430cda01427a742e77e3c433a1c7c3ebd409cd804c45a

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=Apache-2.0
PKG_LICENSE_FILES:=LICENSE

# PKG_BUILD_DEPENDS pre-injected into hostpkg

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-cel-python
	SECTION:=lang
	CATEGORY:=Languages
	SUBMENU:=Python
	TITLE:=Pure Python implementation of Google Common Expression Language
	URL:=https://github.com/cloud-custodian/cel-python
	DEPENDS:=+python3 +python3-lark +python3-google-re2 +python3-pendulum +python3-jmespath +python3-yaml
endef

define Package/python3-cel-python/description
 cel-python is a pure Python implementation of Google's Common Expression
 Language (CEL), used by protovalidate to evaluate protobuf validation
 constraint expressions.
endef

$(eval $(call Py3Package,python3-cel-python))
$(eval $(call BuildPackage,python3-cel-python))
