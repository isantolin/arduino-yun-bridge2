include $(TOPDIR)/rules.mk

PKG_NAME:=python3-serialx
PKG_VERSION:=1.8.1
PKG_RELEASE:=1

PYPI_NAME:=serialx
PKG_SOURCE:=$(PYPI_NAME)-$(PKG_VERSION).tar.gz
PKG_HASH:=957843ec6d1fe91454e182884c29b614fb6d93209668f44197bcbb12d373129b

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=Apache-2.0

PKG_BUILD_DEPENDS:=python-setuptools/host python-setuptools-scm/host python-build/host python-installer/host python-wheel/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Build/Prepare
	$(call Build/Prepare/Default)
	sed -i 's/, "setuptools-rust"//' $(PKG_BUILD_DIR)/pyproject.toml
	sed -i '/ext-modules/,/^$$$$/d' $(PKG_BUILD_DIR)/pyproject.toml
endef

define Package/python3-serialx
	SECTION:=lang
	CATEGORY:=Languages
	SUBMENU:=Python
	TITLE:=Native sync and async serial library
	URL:=https://github.com/puddly/serialx
	DEPENDS:=+python3-light +python3-typing-extensions
endef

define Package/python3-serialx/description
 Serialx provides native synchronous and asynchronous serial APIs, typed
 transports, and modem pin control without depending on pyserial wrappers.
endef

$(eval $(call Py3Package,python3-serialx))
$(eval $(call BuildPackage,python3-serialx))
