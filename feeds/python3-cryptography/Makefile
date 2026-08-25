include $(TOPDIR)/rules.mk

PKG_NAME:=python3-cryptography
PKG_VERSION:=50.0.1
PKG_RELEASE:=1

PYPI_NAME:=cryptography
PKG_HASH:=5dd9bda1c12b4162f6ff568eeb5e0ff956c28d14406e875cfe8a63a2d414ff20

# Se agregaron rust/host y python3-maturin/host
PKG_BUILD_DEPENDS:=libffi/host python3-cffi/host rust/host python3-maturin/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

TARGET_CFLAGS += -I$(STAGING_DIR)/usr/include/python$(PYTHON3_VERSION)

# Se desactiva el aislamiento de dependencias de PEP517 y se pasan variables de OpenSSL/Cargo
PYTHON3_PKG_BUILD_VARS += \
	OPENSSL_NO_VENDOR=1 \
	OPENSSL_DIR=$(STAGING_DIR)/usr \
	OPENSSL_INCLUDE_DIR=$(STAGING_DIR)/usr/include \
	OPENSSL_LIB_DIR=$(STAGING_DIR)/usr/lib \
	CARGO_TARGET_MIPS_UNKNOWN_LINUX_MUSL_LINKER=$(TARGET_CROSS)gcc \
	CARGO_TARGET_MIPS_UNKNOWN_LINUX_MUSL_RUSTFLAGS="-C link-arg=-L$(STAGING_DIR)/usr/lib -C link-arg=-L$(STAGING_DIR)/lib" \
	PYO3_CONFIG_FILE=$(PKG_BUILD_DIR)/pyo3-config.txt \
	PYO3_NO_PYTHON=1

define Build/Prepare
	$(call Build/Prepare/Default)
	sed -i 's|b.finalize_options();|b.finalize_options(); b.include_dirs=[\x27$(STAGING_DIR)/usr/include/python$(PYTHON3_VERSION)\x27];|g' $(PKG_BUILD_DIR)/src/rust/cryptography-cffi/build.rs || true
	# Se relaja la restricción de versión de cffi en pyproject.toml
	sed -i 's/"cffi>=2.0.0[^"]*"/"cffi"/g' $(PKG_BUILD_DIR)/pyproject.toml || true
endef

define Build/Configure
	$(call Build/Configure/Default)
	echo "implementation=CPython" > $(PKG_BUILD_DIR)/pyo3-config.txt
	echo "version=$(PYTHON3_VERSION)" >> $(PKG_BUILD_DIR)/pyo3-config.txt
	echo "shared=true" >> $(PKG_BUILD_DIR)/pyo3-config.txt
	echo "abi3=false" >> $(PKG_BUILD_DIR)/pyo3-config.txt
	echo "pointer_width=32" >> $(PKG_BUILD_DIR)/pyo3-config.txt
	echo "include_dir=$(STAGING_DIR)/usr/include/python$(PYTHON3_VERSION)" >> $(PKG_BUILD_DIR)/pyo3-config.txt
	echo "lib_dir=$(STAGING_DIR)/usr/lib" >> $(PKG_BUILD_DIR)/pyo3-config.txt
	echo "target=mips-unknown-linux-musl" >> $(PKG_BUILD_DIR)/pyo3-config.txt
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
