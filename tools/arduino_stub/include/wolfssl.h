#ifndef WOLFSSL_H
#define WOLFSSL_H

/**
 * @file wolfssl.h
 * @brief Compatibility stub for host-based tests.
 * 
 * In the Arduino environment, the wolfSSL library provides a wolfssl.h header 
 * at the root of the library. When running host tests against the official 
 * wolfSSL repository, we use this stub to include the necessary settings.
 */

#include <wolfssl/wolfcrypt/settings.h>

#endif // WOLFSSL_H
