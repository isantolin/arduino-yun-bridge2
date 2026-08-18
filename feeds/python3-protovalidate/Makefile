include $(TOPDIR)/rules.mk

PKG_NAME:=python3-protovalidate
PKG_VERSION:=1.2.0
PKG_RELEASE:=1

PYPI_NAME:=protovalidate
PKG_HASH:=4cb9a5065f32abc9baf2cf25c5bd09a139f7dffb7b8692f636a8e3e6883bb9a2

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=Apache-2.0
PKG_LICENSE_FILES:=LICENSE

# PKG_BUILD_DEPENDS pre-injected into hostpkg

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

PYTHON3_PKG_BUILD_VARS += \
	SETUPTOOLS_SCM_PRETEND_VERSION=$(PKG_VERSION) \
	HATCH_VCS_RECORD_VERSION=$(PKG_VERSION)

define Build/Prepare
	$(call Build/Prepare/Default)
	sed -i 's/dynamic = \["version"\]/version = "$(PKG_VERSION)"/g' $(PKG_BUILD_DIR)/pyproject.toml 2>/dev/null || true
	sed -i '/\[tool.hatch.version\]/,+2d' $(PKG_BUILD_DIR)/pyproject.toml 2>/dev/null || true
endef

define Package/python3-protovalidate
	SECTION:=lang
	CATEGORY:=Languages
	SUBMENU:=Python
	TITLE:=Protocol Buffer Validation for Python
	URL:=https://github.com/bufbuild/protovalidate-python
	DEPENDS:=+python3 +python3-protobuf +python3-cel-python +python3-google-re2
endef

define Package/python3-protovalidate/description
 protovalidate is the Python runtime implementation of protovalidate,
 evaluating CEL-based validation constraints declared directly in
 protobuf message definitions.
endef

$(eval $(call Py3Package,python3-protovalidate))
$(eval $(call BuildPackage,python3-protovalidate))
