include $(TOPDIR)/rules.mk

PKG_NAME:=python3-shellingham
PKG_VERSION:=1.5.4
PKG_RELEASE:=1

PYPI_NAME:=shellingham
PKG_HASH:=8dbca0739d487e5bd35ab3ca4b36e11c4078f3a234bfce294b0a0291363404de

PKG_BUILD_DEPENDS:=python-setuptools/host python-build/host python-installer/host python-wheel/host

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-shellingham
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Tool to detect the surrounding shell
  URL:=https://github.com/sarugaku/shellingham
  DEPENDS:=+python3
endef

define Package/python3-shellingham/description
  Shellingham detects what shell the current Python executable is running in.
endef

$(eval $(call Py3Package,python3-shellingham))
$(eval $(call BuildPackage,python3-shellingham))
