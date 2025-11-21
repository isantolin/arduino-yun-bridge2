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
#define BRIDGE_SERIAL_SHARED_SECRET "changeme123"
#define BRIDGE_SERIAL_SHARED_SECRET_IS_DEFAULT 1
#else
#define BRIDGE_SERIAL_SHARED_SECRET_IS_DEFAULT 0
#endif

#ifndef BRIDGE_SERIAL_SHARED_SECRET_LEN
#define BRIDGE_SERIAL_SHARED_SECRET_LEN \
  (sizeof(BRIDGE_SERIAL_SHARED_SECRET) - 1)
#endif

#if BRIDGE_SERIAL_SHARED_SECRET_IS_DEFAULT && \
    !defined(BRIDGE_ALLOW_INSECURE_SERIAL_SECRET)
#error                                                                         \
    "Bridge serial handshake secret must be overridden for production builds"
#endif

#undef BRIDGE_SERIAL_SHARED_SECRET_IS_DEFAULT
