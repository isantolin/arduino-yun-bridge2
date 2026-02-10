include $(TOPDIR)/rules.mk

PKG_NAME:=python3-cryptography
PKG_VERSION:=46.0.4
PKG_RELEASE:=1

PYPI_NAME:=cryptography
PKG_HASH:=skip

include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk
include $(TOPDIR)/feeds/packages/lang/rust/rust-package.mk

define Package/python3-cryptography
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Python cryptography library
  DEPENDS:=+python3 +libopenssl
endef

define Package/python3-cryptography/description
 Cryptographic primitives for Python (Rust backend)
endef

$(eval $(call Py3Package,python3-cryptography))
$(eval $(call BuildPackage,python3-cryptography))
