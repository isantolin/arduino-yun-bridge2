include $(TOPDIR)/rules.mk

PKG_NAME:=python3-protovalidate
PKG_VERSION:=1.2.0
PKG_RELEASE:=1

PYPI_NAME:=protovalidate
PKG_HASH:=4cb9a5065f32abc9baf2cf25c5bd09a139f7dffb7b8692f636a8e3e6883bb9a2

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=Apache-2.0
PKG_LICENSE_FILES:=LICENSE

PKG_BUILD_DEPENDS:=python3-setuptools/host python3-build/host python3-installer/host python3-wheel/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Build/Prepare
	$(call Build/Prepare/Default)
	python3 -c "import pathlib; \
p1 = pathlib.Path('$(PKG_BUILD_DIR)/setup.py'); \
p1.write_text('from setuptools import setup\nsetup(\n    name=\"protovalidate\",\n    version=\"$(PKG_VERSION)\",\n    packages=[\"protovalidate\", \"protovalidate.internal\", \"buf\", \"buf.validate\", \"cel\", \"cel.expr\"],\n    package_dir={\"buf\": \"gen/buf\", \"cel\": \"gen/cel\"},\n)\n', encoding='utf-8'); \
p2 = pathlib.Path('$(PKG_BUILD_DIR)/pyproject.toml'); \
p2.write_text('[build-system]\nrequires = [\"setuptools>=61.0\"]\nbuild-backend = \"setuptools.build_meta\"\n\n[project]\nname = \"protovalidate\"\nversion = \"$(PKG_VERSION)\"\ndescription = \"Protocol Buffer Validation for Python\"\nreadme = \"README.md\"\nrequires-python = \">=3.10\"\n\n[tool.setuptools]\npackages = [\"protovalidate\", \"protovalidate.internal\", \"buf\", \"buf.validate\", \"cel\", \"cel.expr\"]\n\n[tool.setuptools.package-dir]\nbuf = \"gen/buf\"\ncel = \"gen/cel\"\n', encoding='utf-8')"
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
