include $(TOPDIR)/rules.mk

PKG_NAME:=python3-serialx
PKG_VERSION:=1.10.0
PKG_RELEASE:=1

PYPI_NAME:=serialx
PKG_HASH:=9b69e8a31059bfbfff74186ea9da6790d0a21bb107451a833c10789e58aa368c

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=Apache-2.0

# PKG_BUILD_DEPENDS pre-injected into hostpkg

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

PYTHON3_PKG_BUILD_VARS += \
	SETUPTOOLS_SCM_PRETEND_VERSION=$(PKG_VERSION) \
	PYO3_CONFIG_FILE=$(PKG_BUILD_DIR)/pyo3-config.txt \
	PYO3_NO_PYTHON=1

define Build/Prepare
	$(call Build/Prepare/Default)
	sed -i 's/"setuptools-scm\[simple\]>=8",//g' $(PKG_BUILD_DIR)/pyproject.toml
	sed -i 's/dynamic = \["version"\]/version = "$(PKG_VERSION)"/g' $(PKG_BUILD_DIR)/pyproject.toml
endef

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
