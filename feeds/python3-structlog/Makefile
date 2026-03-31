include $(TOPDIR)/rules.mk

PKG_NAME:=python3-structlog
PKG_VERSION:=25.5.0
PKG_RELEASE:=1

PYPI_NAME:=structlog
PKG_SOURCE:=$(PYPI_NAME)-$(PKG_VERSION).tar.gz
PKG_HASH:=098522a3bebed9153d4570c6d0288abf80a031dfdb2048d59a49e9dc2190fc98

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=Apache-2.0+MIT
PKG_LICENSE_FILES:=LICENSE

PKG_BUILD_DEPENDS:=python-hatch-vcs/host python-hatchling/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Build/Prepare
	$(call Build/Prepare/Default)
	sed -i "/hatch-fancy-pypi-readme/d" $(PKG_BUILD_DIR)/pyproject.toml
	sed -i "/\[tool.hatch.metadata.hooks.fancy-pypi-readme\]/,/\]/d" $(PKG_BUILD_DIR)/pyproject.toml
endef

define Package/python3-structlog
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Structured logging for Python
  URL:=https://github.com/hynek/structlog
  DEPENDS:=+python3-light
endef

define Package/python3-structlog/description
  structlog makes logging in Python faster, less painful, and more powerful
  by adding structure to your log entries. It can output structured JSON for
  production and human-readable colored output for development.
endef

$(eval $(call Py3Package,python3-structlog))
$(eval $(call BuildPackage,python3-structlog))
