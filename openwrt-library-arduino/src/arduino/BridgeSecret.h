#pragma once

// Optional shared secret used to authenticate the serial handshake.
//
// Projects may override `BRIDGE_SERIAL_SHARED_SECRET` (and optionally
// `BRIDGE_SERIAL_SHARED_SECRET_LEN`) before including `Bridge.h` to embed
// a compile-time secret on the MCU. The default placeholder is
// "changeme123"; make sure to replace it in production and configure the
// Linux daemon with the same value.
//
// Example (Arduino sketch):
//
//   #define BRIDGE_SERIAL_SHARED_SECRET "myS3cret"
//   #include <Bridge.h>
//
// The accompanying Linux daemon must be configured with the exact same
// secret for the handshake to complete successfully.

#ifndef BRIDGE_SERIAL_SHARED_SECRET
#if __has_include("BridgeSecret.local.h")
#include "BridgeSecret.local.h"
#endif
#endif

#ifndef BRIDGE_SERIAL_SHARED_SECRET
#define BRIDGE_SERIAL_SHARED_SECRET \
  "755142925659b6f5d3ab00b7b280d72fc1cc17f0dad9f52fff9f65efd8caf8e3"
#endif

#ifndef BRIDGE_SERIAL_SHARED_SECRET_LEN
#define BRIDGE_SERIAL_SHARED_SECRET_LEN \
  (sizeof(BRIDGE_SERIAL_SHARED_SECRET) - 1)
#endif

#ifdef BRIDGE_ALLOW_INSECURE_SERIAL_SECRET
#warning "BRIDGE_ALLOW_INSECURE_SERIAL_SECRET is deprecated and ignored"
#endif
