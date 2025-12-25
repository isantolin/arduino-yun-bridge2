include $(TOPDIR)/rules.mk

PKG_NAME:=python3-psutil
PKG_VERSION:=7.2.0
PKG_RELEASE:=1

PYPI_NAME:=psutil
PKG_HASH:=2e4f8e1552f77d14dc96fb0f6240c5b34a37081c0889f0853b3b29a496e5ef64
PKG_SOURCE:=$(PYPI_NAME)-$(PKG_VERSION).tar.gz
PKG_SOURCE_URL:=https://files.pythonhosted.org/packages/source/p/psutil/

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=BSD-3-Clause
PKG_LICENSE_FILES:=LICENSE

include $(INCLUDE_DIR)/package.mk
include $(INCLUDE_DIR)/../feeds/packages/lang/python/python3-package.mk

define Package/python3-psutil
  SECTION:=lang
  CATEGORY:=Languages
  SUBMENU:=Python
  TITLE:=Cross-platform process and system utilities
  URL:=https://github.com/giampaolo/psutil
  DEPENDS:=+python3
endef

define Package/python3-psutil/description
  psutil is a cross-platform library for retrieving information on
  running processes and system utilization (CPU, memory, disks, network,
  sensors) in Python.
endef

$(eval $(call Py3Package,python3-psutil))
$(eval $(call BuildPackage,python3-psutil))
