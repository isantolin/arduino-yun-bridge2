include $(TOPDIR)/rules.mk

PKG_NAME:=python-pydantic-core
PKG_VERSION:=2.23.4
PKG_RELEASE:=1

PYPI_NAME:=pydantic_core
PKG_HASH:=2584f7cf844ac4d970fba483a717dbe10c1c1c96a969bf65d61ffe94df1b2863

PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE
PKG_MAINTAINER:=Ignacio Santolin <ignaciosantolin@example.com>

PKG_BUILD_DEPENDS:=rust/host python-maturin/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-pydantic-core
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Core validation and serialization library used by pydantic
  URL:=https://github.com/pydantic/pydantic-core
  DEPENDS:=+python3-light +python3-typing-extensions
endef

define Package/python3-pydantic-core/description
 Rust-powered validation and serialization primitives consumed by pydantic v2.
endef

$(eval $(call Py3Package,python3-pydantic-core))
$(eval $(call BuildPackage,python3-pydantic-core))
