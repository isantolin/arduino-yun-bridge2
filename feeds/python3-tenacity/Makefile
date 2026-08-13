include $(TOPDIR)/rules.mk

PKG_NAME:=python3-tenacity
PKG_VERSION:=9.1.4
PKG_RELEASE:=1

PYPI_NAME:=tenacity
PKG_HASH:=adb31d4c263f2bd041081ab33b498309a57c77f9acf2db65aadf0898179cf93a

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=Apache-2.0
PKG_LICENSE_FILES:=LICENSE

# PKG_BUILD_DEPENDS pre-injected into hostpkg

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

PYTHON3_PKG_BUILD_VARS += \
	SETUPTOOLS_SCM_PRETEND_VERSION=$(PKG_VERSION)

define Build/Prepare
	$(call Build/Prepare/Default)
	sed -i 's/"setuptools_scm\[toml\]>=[0-9.]*",//g' $(PKG_BUILD_DIR)/pyproject.toml 2>/dev/null || true
	sed -i 's/"setuptools_scm",//g' $(PKG_BUILD_DIR)/pyproject.toml 2>/dev/null || true
	sed -i 's/dynamic = \["version"\]/version = "$(PKG_VERSION)"/g' $(PKG_BUILD_DIR)/pyproject.toml 2>/dev/null || true
endef

define Package/python3-tenacity
	SECTION:=lang
	CATEGORY:=Languages
	SUBMENU:=Python
	TITLE:=Retry library for Python
	URL:=https://github.com/jd/tenacity
	DEPENDS:=+python3-light
endef

define Package/python3-tenacity/description
 Tenacity is an Apache 2.0 licensed general-purpose retrying library,
 written in Python, to simplify the task of adding retry behavior to
 just about anything.
endef

$(eval $(call Py3Package,python3-tenacity))
$(eval $(call BuildPackage,python3-tenacity))
