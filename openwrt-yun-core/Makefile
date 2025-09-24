
include $(TOPDIR)/rules.mk

PKG_NAME:=openwrt-yun-core
PKG_VERSION:=1.0
PKG_RELEASE:=1

include $(INCLUDE_DIR)/package.mk

define Package/$(PKG_NAME)
  SECTION:=utils
  CATEGORY:=Utilities
	TITLE:=Yun Core Scripts and Configs (system Python only)
endef

define Package/$(PKG_NAME)/description
 Core scripts and configuration for Arduino Yun v2. Python dependencies are installed system-wide (no venv).
endef

define Build/Compile
	# Nothing to compile
endef

define Package/$(PKG_NAME)/install
	$(INSTALL_DIR) $(1)/etc/yunbridge
	$(INSTALL_DATA) ./yunbridge.files $(1)/etc/yunbridge/
	$(INSTALL_DIR) $(1)/etc/config
	$(INSTALL_CONF) ./99-yunbridge-ttyath0.conf $(1)/etc/config/yunbridge-ttyath0
	$(INSTALL_DIR) $(1)/etc/init.d
	$(INSTALL_BIN) ./yunbridge.init $(1)/etc/init.d/yunbridge
	$(INSTALL_DIR) $(1)/www/cgi-bin
	[ -f ./scripts/pin_rest_cgi.py ] && $(INSTALL_BIN) ./scripts/pin_rest_cgi.py $(1)/www/cgi-bin/pin || true
	$(INSTALL_DIR) $(1)/usr/bin
	[ -d ./scripts ] && for f in ./scripts/*; do $(INSTALL_BIN) $$f $(1)/usr/bin/$$f; done || true
	# Dummy file to force OpenWRT to use custom postinst
	$(INSTALL_DIR) $(1)/etc
	echo "yun-core" > $(1)/etc/yun-core-dummy
endef

$(eval $(call BuildPackage,$(PKG_NAME)))

# Custom postinst block
define Package/openwrt-yun-core/postinst
#!/bin/sh
# postinst script for openwrt-yun-core
SWAPFILE="/overlay/swapfile"
SWAPSIZE_MB=512
if [ ! -f "$SWAPFILE" ]; then
	logger -t yun-core "Creating swap file at $SWAPFILE (${SWAPSIZE_MB}MB) ..."
	if ! dd if=/dev/zero of="$SWAPFILE" bs=1M count=$SWAPSIZE_MB; then
		logger -t yun-core "[ERROR] Failed to create swap file."
		exit 1
	fi
	if ! mkswap "$SWAPFILE"; then
		logger -t yun-core "[ERROR] Failed to format swap file."
		exit 1
	fi
	chmod 600 "$SWAPFILE"
fi
if ! swapon -s | grep -q "$SWAPFILE"; then
	logger -t yun-core "Activating swap file $SWAPFILE ..."
	if ! swapon "$SWAPFILE"; then
		logger -t yun-core "[ERROR] Failed to activate swap file."
		exit 1
	fi
fi
if ! grep -q "$SWAPFILE" /etc/fstab; then
	echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
fi
SD_MOUNT="/mnt/sda1"
VENV_PATH="$SD_MOUNT/pyenv"
PYTHON="$VENV_PATH/bin/python3"
PIP="$VENV_PATH/bin/pip"
if [ ! -d "$VENV_PATH" ]; then
	logger -t yun-core "Creating Python virtual environment at $VENV_PATH ..."
	if ! python3 -m venv "$VENV_PATH"; then
		logger -t yun-core "[ERROR] Could not create Python venv on SD card."
		exit 1
	fi
fi
if [ -x "$PYTHON" ]; then
	logger -t yun-core "Activating venv and upgrading pip ..."
	. "$VENV_PATH/bin/activate"
	if ! "$PIP" install --no-cache-dir --force-reinstall --upgrade pip; then
		logger -t yun-core "[ERROR] Could not upgrade pip in venv."
		exit 1
	fi
fi
if [ -x /etc/init.d/yunbridge ]; then
	logger -t yun-core "Enabling and starting yunbridge daemon ..."
	/etc/init.d/yunbridge enable
	/etc/init.d/yunbridge start
else
	logger -t yun-core "[WARNING] yunbridge init script not found at /etc/init.d/yunbridge."
fi
endef

# Correct OpenWRT postinst block
define Package/openwrt-yun-core/postinst
#!/bin/sh
# postinst script for openwrt-yun-core
SWAPFILE="/overlay/swapfile"
SWAPSIZE_MB=512
if [ ! -f "$SWAPFILE" ]; then
	logger -t yun-core "Creating swap file at $SWAPFILE (${SWAPSIZE_MB}MB) ..."
	if ! dd if=/dev/zero of="$SWAPFILE" bs=1M count=$SWAPSIZE_MB; then
		logger -t yun-core "[ERROR] Failed to create swap file."
		exit 1
	fi
	if ! mkswap "$SWAPFILE"; then
		logger -t yun-core "[ERROR] Failed to format swap file."
		exit 1
	fi
	chmod 600 "$SWAPFILE"
fi
if ! swapon -s | grep -q "$SWAPFILE"; then
	logger -t yun-core "Activating swap file $SWAPFILE ..."
	if ! swapon "$SWAPFILE"; then
		logger -t yun-core "[ERROR] Failed to activate swap file."
		exit 1
	fi
fi
if ! grep -q "$SWAPFILE" /etc/fstab; then
	echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
fi
SD_MOUNT="/mnt/sda1"
VENV_PATH="$SD_MOUNT/pyenv"
PYTHON="$VENV_PATH/bin/python3"
PIP="$VENV_PATH/bin/pip"
if [ ! -d "$VENV_PATH" ]; then
	logger -t yun-core "Creating Python virtual environment at $VENV_PATH ..."
	if ! python3 -m venv "$VENV_PATH"; then
		logger -t yun-core "[ERROR] Could not create Python venv on SD card."
		exit 1
	fi
fi
if [ -x "$PYTHON" ]; then
	logger -t yun-core "Activating venv and upgrading pip ..."
	. "$VENV_PATH/bin/activate"
	if ! "$PIP" install --no-cache-dir --force-reinstall --upgrade pip; then
		logger -t yun-core "[ERROR] Could not upgrade pip in venv."
		exit 1
	fi
fi
if [ -x /etc/init.d/yunbridge ]; then
	logger -t yun-core "Enabling and starting yunbridge daemon ..."
	/etc/init.d/yunbridge enable
	/etc/init.d/yunbridge start
else
	logger -t yun-core "[WARNING] yunbridge init script not found at /etc/init.d/yunbridge."
fi
endef

# Correct OpenWRT postinst block with real script
define Package/openwrt-yun-core/postinst
#!/bin/sh
# postinst script for openwrt-yun-core
SWAPFILE="/overlay/swapfile"
SWAPSIZE_MB=512
if [ ! -f "$SWAPFILE" ]; then
	logger -t yun-core "Creating swap file at $SWAPFILE (${SWAPSIZE_MB}MB) ..."
	if ! dd if=/dev/zero of="$SWAPFILE" bs=1M count=$SWAPSIZE_MB; then
		logger -t yun-core "[ERROR] Failed to create swap file."
		exit 1
	fi
	if ! mkswap "$SWAPFILE"; then
		logger -t yun-core "[ERROR] Failed to format swap file."
		exit 1
	fi
	chmod 600 "$SWAPFILE"
fi
if ! swapon -s | grep -q "$SWAPFILE"; then
	logger -t yun-core "Activating swap file $SWAPFILE ..."
	if ! swapon "$SWAPFILE"; then
		logger -t yun-core "[ERROR] Failed to activate swap file."
		exit 1
	fi
fi
if ! grep -q "$SWAPFILE" /etc/fstab; then
	echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
fi
SD_MOUNT="/mnt/sda1"
VENV_PATH="$SD_MOUNT/pyenv"
PYTHON="$VENV_PATH/bin/python3"
PIP="$VENV_PATH/bin/pip"
if [ ! -d "$VENV_PATH" ]; then
	logger -t yun-core "Creating Python virtual environment at $VENV_PATH ..."
	if ! python3 -m venv "$VENV_PATH"; then
		logger -t yun-core "[ERROR] Could not create Python venv on SD card."
		exit 1
	fi
fi
if [ -x "$PYTHON" ]; then
	logger -t yun-core "Activating venv and upgrading pip ..."
	. "$VENV_PATH/bin/activate"
	if ! "$PIP" install --no-cache-dir --force-reinstall --upgrade pip; then
		logger -t yun-core "[ERROR] Could not upgrade pip in venv."
		exit 1
	fi
fi
if [ -x /etc/init.d/yunbridge ]; then
	logger -t yun-core "Enabling and starting yunbridge daemon ..."
	/etc/init.d/yunbridge enable
	/etc/init.d/yunbridge start
else
	logger -t yun-core "[WARNING] yunbridge init script not found at /etc/init.d/yunbridge."
fi
endef

# Embed real postinst script in package
define Package/openwrt-yun-core/postinst
#!/bin/sh
# postinst script for openwrt-yun-core
# Ensures swap file is present and active, and Python venv is set up on SD card

# --- Swap Setup ---
SWAPFILE="/overlay/swapfile"
SWAPSIZE_MB=512
if [ ! -f "$SWAPFILE" ]; then
	logger -t yun-core "Creating swap file at $SWAPFILE (${SWAPSIZE_MB}MB) ..."
	if ! dd if=/dev/zero of="$SWAPFILE" bs=1M count=$SWAPSIZE_MB; then
		logger -t yun-core "[ERROR] Failed to create swap file."
		exit 1
	fi
	if ! mkswap "$SWAPFILE"; then
		logger -t yun-core "[ERROR] Failed to format swap file."
		exit 1
	fi
	chmod 600 "$SWAPFILE"
fi
if ! swapon -s | grep -q "$SWAPFILE"; then
	logger -t yun-core "Activating swap file $SWAPFILE ..."
	if ! swapon "$SWAPFILE"; then
		logger -t yun-core "[ERROR] Failed to activate swap file."
		exit 1
	fi
fi
# Ensure swap is enabled on boot
if ! grep -q "$SWAPFILE" /etc/fstab; then
	echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
fi

# --- Python venv Setup ---
SD_MOUNT="/mnt/sda1"
VENV_PATH="$SD_MOUNT/pyenv"
PYTHON="$VENV_PATH/bin/python3"
PIP="$VENV_PATH/bin/pip"

if [ ! -d "$VENV_PATH" ]; then
	logger -t yun-core "Creating Python virtual environment at $VENV_PATH ..."
	if ! python3 -m venv "$VENV_PATH"; then
		logger -t yun-core "[ERROR] Could not create Python venv on SD card."
		exit 1
	fi
fi

if [ -x "$PYTHON" ]; then
	logger -t yun-core "Activating venv and upgrading pip ..."
	# shellcheck disable=SC1090
	. "$VENV_PATH/bin/activate"
	if ! "$PIP" install --no-cache-dir --force-reinstall --upgrade pip; then
		logger -t yun-core "[ERROR] Could not upgrade pip in venv."
		exit 1
	fi
fi
# --- Daemon Enable & Start ---
if [ -x /etc/init.d/yunbridge ]; then
	logger -t yun-core "Enabling and starting yunbridge daemon ..."
	/etc/init.d/yunbridge enable
	/etc/init.d/yunbridge start
else
	logger -t yun-core "[WARNING] yunbridge init script not found at /etc/init.d/yunbridge."
fi
endef

