include $(TOPDIR)/rules.mk

PKG_NAME:=python3-heapdict
PKG_VERSION:=1.0.1
PKG_RELEASE:=1

PYPI_NAME:=HeapDict
PKG_SOURCE:=$(PYPI_NAME)-$(PKG_VERSION).tar.gz
PKG_HASH:=8495f57b3e03d8e46d5f1b2cc62ca881aca392fd5cc048dc0aa2e1a6d23ecdb6

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=BSD-3-Clause
PKG_LICENSE_FILES:=LICENSE

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-heapdict
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=A heap with dictionary-like interface
  URL:=https://github.com/stutzbach/heapdict
  DEPENDS:=+python3-light
endef

define Package/python3-heapdict/description
  HeapDict implements the MutableMapping ABC, meaning it can be used
  like a regular dictionary.  However, it is a priority queue.
endef

$(eval $(call Py3Package,python3-heapdict))
$(eval $(call BuildPackage,python3-heapdict))
