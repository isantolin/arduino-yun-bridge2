#
# This file is part of Arduino MCU Ecosystem v2.
#
# Copyright (C) 2025 Ignacio Santolin and contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# OpenWRT package Makefile for mcubridge-gateway
#

include $(TOPDIR)/rules.mk

PKG_NAME:=mcubridge-gateway
PKG_VERSION:=2.8.5
PKG_RELEASE:=1
PKG_LICENSE:=GPL-3.0-or-later
PKG_LICENSE_FILES:=LICENSE
PKG_MAINTAINER:=Ignacio Santolin <ignacio.santolin@gmail.com>

PKG_BUILD_DIR := $(BUILD_DIR)/$(PKG_NAME)

include $(INCLUDE_DIR)/package.mk

define Package/$(PKG_NAME)
	SECTION:=utils
	CATEGORY:=Utilities
	TITLE:=MCU Bridge Cloud Gateway
	PKG_ARCH:=all
	# AUTO-GENERATED RUNTIME DEPENDS BEGIN
	DEPENDS+= \
		python3 \
		python3-protobuf \
		python3-h2 \
		python3-grpclib
	# AUTO-GENERATED RUNTIME DEPENDS END
endef

define Package/$(PKG_NAME)/description
 Protobuf Cloud Gateway service (gRPC over HTTP/2) for MCU Bridge v2.
endef

define Build/Prepare
	rm -rf $(PKG_BUILD_DIR)
	mkdir -p $(PKG_BUILD_DIR)/mcubridge/protocol
	# Copy gateway source
	$(CP) ./gateway.py $(PKG_BUILD_DIR)/mcubridge-gateway
	# Copy protocol stubs from mcubridge MPU package
	$(CP) ../mcubridge/mcubridge/protocol/mcubridge_pb2.py $(PKG_BUILD_DIR)/mcubridge/protocol/
	$(CP) ../mcubridge/mcubridge/protocol/mcubridge_pb2.pyi $(PKG_BUILD_DIR)/mcubridge/protocol/
	$(CP) ../mcubridge/mcubridge/protocol/mcubridge_grpc.py $(PKG_BUILD_DIR)/mcubridge/protocol/
	# Create __init__.py files
	touch $(PKG_BUILD_DIR)/mcubridge/__init__.py
	touch $(PKG_BUILD_DIR)/mcubridge/protocol/__init__.py
endef

define Build/Compile
	python3 -O -m compileall -j 0 -b -f -q $(PKG_BUILD_DIR)/mcubridge
	find $(PKG_BUILD_DIR) -name '__pycache__' -type d -print0 | xargs -0 -r rm -rf || true
endef

define Package/$(PKG_NAME)/install
	$(INSTALL_DIR) $(1)/usr/bin
	$(INSTALL_DIR) $(1)/usr/lib/python3.13/site-packages/mcubridge/protocol

	# Install Gateway Executable
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/mcubridge-gateway $(1)/usr/bin/mcubridge-gateway

	# Install Protocol Stubs for self-contained imports
	$(CP) $(PKG_BUILD_DIR)/mcubridge/__init__.py $(1)/usr/lib/python3.13/site-packages/mcubridge/
	$(CP) $(PKG_BUILD_DIR)/mcubridge/protocol/* $(1)/usr/lib/python3.13/site-packages/mcubridge/protocol/
endef

define Package/$(PKG_NAME)/clean
	rm -rf $(PKG_BUILD_DIR)
endef

$(eval $(call BuildPackage,$(PKG_NAME)))
