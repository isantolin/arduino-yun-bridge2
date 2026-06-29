include $(TOPDIR)/rules.mk

PKG_NAME:=python3-mqtt5
PKG_VERSION:=0.5.0
PKG_RELEASE:=1

PYPI_NAME:=mqtt5
PKG_SOURCE:=mqtt5-$(PKG_VERSION).tar.gz
PKG_HASH:=9d0c5de8faa0d4a5a4b2735edbf497da7f40d2866d9fe81cc45081e2a5c8a3b4

PKG_BUILD_DEPENDS:=python-maturin/host rust/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

TARGET_CFLAGS += -I$(STAGING_DIR)/usr/include/python$(PYTHON3_VERSION)

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
	$(if $(filter be,$(BOARD)),echo "target=mips-unknown-linux-musl" >> $(PKG_BUILD_DIR)/pyo3-config.txt)
endef

define Package/python3-mqtt5
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=MQTTv5 protocol for Python written in Rust
  URL:=https://github.com/empicano/mqtt5
  DEPENDS:=+python3-light
endef

define Package/python3-mqtt5/description
  A high-performance MQTTv5 protocol parser for Python written in Rust.
endef

$(eval $(call Py3Package,python3-mqtt5))
$(eval $(call BuildPackage,python3-mqtt5))
