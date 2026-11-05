include $(TOPDIR)/rules.mk

PKG_NAME:=python3-anyio
PKG_VERSION:=4.16.0
PKG_RELEASE:=1

PYPI_NAME:=anyio
PKG_HASH:=59bb03251907983d1603d27c3087ddb1e93299ea2163adcdba37899a4fb12ffb

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

PYTHON3_PKG_BUILD_VARS += \
	SETUPTOOLS_SCM_PRETEND_VERSION=$(PKG_VERSION)

define Build/Prepare
	$(call Build/Prepare/Default)
	sed -i 's/dynamic = \["version"\]/version = "$(PKG_VERSION)"/g' $(PKG_BUILD_DIR)/pyproject.toml 2>/dev/null || true
	sed -i '/setuptools_scm/d' $(PKG_BUILD_DIR)/pyproject.toml 2>/dev/null || true
	sed -i '/\[tool.setuptools_scm\]/d' $(PKG_BUILD_DIR)/pyproject.toml 2>/dev/null || true
endef

define Package/python3-anyio
	SECTION:=lang
	CATEGORY:=Languages
	SUBMENU:=Python
	TITLE:=High-level concurrency and networking framework
	URL:=https://github.com/agronholm/anyio
	DEPENDS:=+python3-light
endef

define Package/python3-anyio/description
 High-level concurrency and networking framework on top of asyncio or Trio.
endef

$(eval $(call Py3Package,python3-anyio))
$(eval $(call BuildPackage,python3-anyio))
