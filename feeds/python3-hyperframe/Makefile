include $(TOPDIR)/rules.mk

PKG_NAME:=python3-hyperframe
PKG_VERSION:=6.1.0
PKG_RELEASE:=1

PYPI_NAME:=hyperframe
PKG_HASH:=f630908a00854a7adeabd6382b43923a4c4cd4b821fcb527e6ab9e15382a3b08

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-hyperframe
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=HTTP/2 framing layer for Python
  URL:=https://github.com/python-hyper/hyperframe
  DEPENDS:=+python3-light
endef

define Package/python3-hyperframe/description
  Pure-Python HTTP/2 framing logic.
endef

$(eval $(call Py3Package,python3-hyperframe))
$(eval $(call BuildPackage,python3-hyperframe))
