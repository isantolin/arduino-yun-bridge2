
include $(TOPDIR)/rules.mk

PKG_NAME:=openwrt-yun-core
PKG_VERSION:=1.0
PKG_RELEASE:=1

PKG_BUILD_DIR := $(BUILD_DIR)/$(PKG_NAME)-$(PKG_VERSION)
HOST_PYTHON3 := $(STAGING_DIR_HOSTPKG)/bin/python3
PYTHON3 := $(if $(wildcard $(HOST_PYTHON3)),$(HOST_PYTHON3),python3)

include $(INCLUDE_DIR)/package.mk

define Package/$(PKG_NAME)
  SECTION:=utils
  CATEGORY:=Utilities
	TITLE:=Yun Core Scripts and Configs (system Python only)
	DEPENDS:=+python3 +python3-uci +mosquitto-client
endef

define Package/$(PKG_NAME)/description
 Core scripts and configuration for Arduino Yun v2. Python dependencies are installed system-wide (no venv).
endef

define Build/Prepare
	rm -rf $(PKG_BUILD_DIR)
	mkdir -p $(PKG_BUILD_DIR)
	$(CP) ./yunbridge.files $(PKG_BUILD_DIR)/
	# Copy uci-defaults scripts for first-boot configuration
	$(CP) ./uci-defaults/95-yunbridge-silence-kernel-console $(PKG_BUILD_DIR)/
	$(CP) ./uci-defaults/96-yunbridge-flash-protection-paths $(PKG_BUILD_DIR)/
	$(CP) ./uci-defaults/97-yunbridge-default-secret $(PKG_BUILD_DIR)/
	$(CP) ./uci-defaults/98-yunbridge-default-serial $(PKG_BUILD_DIR)/
	$(CP) ./uci-defaults/99-yunbridge-disable-askconsole $(PKG_BUILD_DIR)/
	[ -d ./scripts ] && $(CP) -r ./scripts $(PKG_BUILD_DIR)/
endef

define Build/Compile
	[ ! -d $(PKG_BUILD_DIR)/scripts ] || $(PYTHON3) -m compileall -q -b $(PKG_BUILD_DIR)/scripts
	[ ! -d $(PKG_BUILD_DIR)/scripts ] || find $(PKG_BUILD_DIR)/scripts -name '__pycache__' -type d -print0 | xargs -0 -r rm -rf
endef

define Package/$(PKG_NAME)/install
	$(INSTALL_DIR) $(1)/etc/yunbridge
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/yunbridge.files $(1)/etc/yunbridge/
	$(INSTALL_DIR) $(1)/etc/uci-defaults
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/95-yunbridge-silence-kernel-console $(1)/etc/uci-defaults/95-yunbridge-silence-kernel-console
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/96-yunbridge-flash-protection-paths $(1)/etc/uci-defaults/96-yunbridge-flash-protection-paths
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/97-yunbridge-default-secret $(1)/etc/uci-defaults/97-yunbridge-default-secret
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/98-yunbridge-default-serial $(1)/etc/uci-defaults/98-yunbridge-default-serial
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/99-yunbridge-disable-askconsole $(1)/etc/uci-defaults/99-yunbridge-disable-askconsole
	$(INSTALL_DIR) $(1)/www/cgi-bin
	$(INSTALL_DIR) $(1)/usr/bin
	[ -d $(PKG_BUILD_DIR)/scripts ] && find $(PKG_BUILD_DIR)/scripts -type f -print0 | xargs -0 -I {} $(INSTALL_BIN) "{}" $(1)/usr/bin/ || true
endef

$(eval $(call BuildPackage,openwrt-yun-core))

