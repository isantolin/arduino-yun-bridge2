include $(TOPDIR)/rules.mk

PKG_NAME:=python3-cryptography
PKG_VERSION:=46.0.6
PKG_RELEASE:=1

PYPI_NAME:=cryptography
PKG_HASH:=27550628a518c5c6c903d84f637fbecf287f6cb9ced3804838a1295dc1fd0759

PKG_BUILD_DEPENDS:=libffi/host python-maturin/host python-cffi/host rust/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

TARGET_CFLAGS += -I$(STAGING_DIR)/usr/include/python$(PYTHON3_VERSION)

PYTHON3_PKG_BUILD_VARS += \
	PYO3_CONFIG_FILE=$(PKG_BUILD_DIR)/pyo3-config.txt \
	PYO3_NO_PYTHON=1

define Build/Prepare
	$(call Build/Prepare/Default)
	sed -i 's|b.finalize_options();|b.finalize_options(); b.include_dirs=[\x27$(STAGING_DIR)/usr/include/python$(PYTHON3_VERSION)\x27];|g' $(PKG_BUILD_DIR)/src/rust/cryptography-cffi/build.rs
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

define Package/python3-cryptography
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Python cryptography library
  DEPENDS:=+python3 +libopenssl +python3-cffi
endef

define Package/python3-cryptography/description
 Cryptographic primitives for Python (Rust backend)
endef

$(eval $(call Py3Package,python3-cryptography))
$(eval $(call BuildPackage,python3-cryptography))
