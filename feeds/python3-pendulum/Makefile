include $(TOPDIR)/rules.mk

PKG_NAME:=python3-pendulum
PKG_VERSION:=3.2.0
PKG_RELEASE:=1

PYPI_NAME:=pendulum
PKG_HASH:=e80feda2d10fa3ff8b1526715f7d33dcb7e08494b3088f2c8a3ac92d4a4331ce

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE

# PKG_BUILD_DEPENDS pre-injected into hostpkg

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
	# [FIX] Force maturin's rust target for cross-compilation
	$(if $(filter be,$(BOARD)),echo "target=mips-unknown-linux-musl" >> $(PKG_BUILD_DIR)/pyo3-config.txt)
endef

define Package/python3-pendulum
	SECTION:=lang
	CATEGORY:=Languages
	SUBMENU:=Python
	TITLE:=Python datetimes made easy (Rust backend)
	URL:=https://github.com/sdispater/pendulum
	DEPENDS:=+python3 +python3-dateutil +python3-tzdata
endef

define Package/python3-pendulum/description
 Pendulum is a drop-in replacement for the standard datetime class with a
 cleaner API, proper DST handling, and a Rust-accelerated core. Required
 by cel-python (used by protovalidate) for CEL timestamp/duration types.
endef

$(eval $(call Py3Package,python3-pendulum))
$(eval $(call BuildPackage,python3-pendulum))
