include $(TOPDIR)/rules.mk

PKG_NAME:=python-typing-inspection
PKG_VERSION:=0.4.2
PKG_RELEASE:=1

PYPI_NAME:=typing_inspection
PKG_HASH:=ba561c48a67c5958007083d386c3295464928b01faa735ab8547c5692e87f464

PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE
PKG_MAINTAINER:=Ignacio Santolin <ignaciosantolin@example.com>
PKG_BUILD_DEPENDS:=python-hatchling/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Build/Prepare
	$(call Build/Prepare/Default)
	$(SED) '/Programming Language :: Python :: 3\.14/d' $(PKG_BUILD_DIR)/pyproject.toml
endef

define Package/python3-typing-inspection
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Utilities for introspecting typing constructs
  URL:=https://pypi.org/project/typing-inspection/
  DEPENDS:=+python3-light
endef

define Package/python3-typing-inspection/description
 Small helper library that inspects typing module constructs (e.g. Annotated, GenericAlias).
endef

$(eval $(call Py3Package,python3-typing-inspection))
$(eval $(call BuildPackage,python3-typing-inspection))
