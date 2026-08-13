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

define Build/Prepare
	$(call Build/Prepare/Default)
	python3 -c "import re, pathlib; p = pathlib.Path('$(PKG_BUILD_DIR)/pyproject.toml'); c = p.read_text(); c = c.replace('dynamic = [\"version\"]', 'version = \"$(PKG_VERSION)\"').replace('\"hatch-vcs\",', ''); c = re.sub(r'\[tool\.hatch\.version\][\s\S]*?(?=\n\[|\Z)', '', c); p.write_text(c)"
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
