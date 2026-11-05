include $(TOPDIR)/rules.mk

PKG_NAME:=python3-pigweed
PKG_VERSION:=0.0.24
PKG_RELEASE:=1

PYPI_NAME:=pigweed
PKG_HASH:=98ef14eb3c69a13d3a771d8317d82040b7915c7b83ac3e4792cdd57aa933c96c

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=Apache-2.0
PKG_LICENSE_FILES:=LICENSE

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-pigweed
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Google Pigweed Python modules
  URL:=https://pigweed.dev/
  DEPENDS:=+python3-light +python3-protobuf
endef

define Package/python3-pigweed/description
  Python modules for Pigweed RPC, HDLC, tokenizer, and embedded tooling.
endef

$(eval $(call Py3Package,python3-pigweed))
$(eval $(call BuildPackage,python3-pigweed))
