"""MCU Bridge Package Initialisation."""

from __future__ import annotations

import os

# Prevent OpenSSL 3 from attempting to load the deprecated legacy provider
# on systems (such as OpenWrt / embedded targets) where it is not compiled or available.
os.environ.setdefault("CRYPTOGRAPHY_OPENSSL_NO_LEGACY", "1")

__version__ = "2.8.6"
