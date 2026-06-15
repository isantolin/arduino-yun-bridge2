include $(TOPDIR)/rules.mk

PKG_NAME:=python3-serialx
PKG_VERSION:=1.8.2
PKG_RELEASE:=1

PYPI_NAME:=serialx
PKG_SOURCE:=$(PYPI_NAME)-$(PKG_VERSION).tar.gz
PKG_HASH:=90c912ec8ceb4ae26a7dca158ca21e5bd8d73ddfc93c5e8d8b101a22ddccbb9a

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=Apache-2.0

PKG_BUILD_DEPENDS:=python-setuptools/host python-setuptools-rust/host python-setuptools-scm/host python-build/host python-installer/host python-wheel/host rust/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

PYTHON3_PKG_BUILD_VARS += \
	PYO3_CONFIG_FILE=$(PKG_BUILD_DIR)/pyo3-config.txt \
	PYO3_NO_PYTHON=1

define Build/Configure
	$(call Build/Configure/Default)
	echo "implementation=CPython" > $(PKG_BUILD_DIR)/pyo3-config.txt
	echo "version=$(PYTHON3_VERSION)" >> $(PKG_BUILD_DIR)/pyo3-config.txt
	echo "shared=true" >> $(PKG_BUILD_DIR)/pyo3-config.txt
	echo "abi3=false" >> $(PKG_BUILD_DIR)/pyo3-config.txt
	echo "include_dir=$(STAGING_DIR)/usr/include/python$(PYTHON3_VERSION)" >> $(PKG_BUILD_DIR)/pyo3-config.txt
	echo "lib_dir=$(STAGING_DIR)/usr/lib" >> $(PKG_BUILD_DIR)/pyo3-config.txt
	# [FIX] Force maturins rust target for cross-compilation
	$(if $(filter be,$(BOARD)),echo "target=mips-unknown-linux-musl" >> $(PKG_BUILD_DIR)/pyo3-config.txt)
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
