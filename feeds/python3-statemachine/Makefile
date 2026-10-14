include $(TOPDIR)/rules.mk

PKG_NAME:=python3-statemachine
PKG_VERSION:=3.2.2
PKG_RELEASE:=1

PYPI_NAME:=python_statemachine
PKG_HASH:=5968c5e07a537f7531b8b1d0a0d5067a3452c88941fa0ac6c244870d48f78325

PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

define Package/python3-statemachine
	SECTION:=lang
	CATEGORY:=Languages
	SUBMENU:=Python
	TITLE:=Python Finite State Machines made easy
	URL:=https://github.com/fgmacedo/python-statemachine
	DEPENDS:=+python3-light
endef

define Package/python3-statemachine/description
 Python Finite State Machines made easy. Deterministic FSM implementation
 matching SIL-2 MCU architecture.
endef

$(eval $(call Py3Package,python3-statemachine))
$(eval $(call BuildPackage,python3-statemachine))
