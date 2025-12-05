include $(TOPDIR)/rules.mk

PKG_NAME:=python-pydantic
PKG_VERSION:=2.12.5
PKG_RELEASE:=1

PKG_BUILD_DEPENDS:=python-hatchling/host python-hatch-fancy-pypi-readme/host

PYPI_NAME:=pydantic
PKG_HASH:=4d351024c75c0f085a9febbb665ce8c0c6ec5d30e903bdb6394b7ede26aebb49

PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE
PKG_MAINTAINER:=Ignacio Santolin <ignaciosantolin@example.com>

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-pydantic
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Data validation and settings management using Python type hints
  URL:=https://github.com/pydantic/pydantic
  DEPENDS:=+python3-light +python3-annotated-types +python3-pydantic-core +python3-typing-extensions
endef

define Package/python3-pydantic/description
 Pydantic enforces type hints at runtime providing user friendly errors.
endef

$(eval $(call Py3Package,python3-pydantic))
$(eval $(call BuildPackage,python3-pydantic))
