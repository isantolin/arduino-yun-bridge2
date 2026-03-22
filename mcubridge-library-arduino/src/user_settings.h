/* 
 * [WOLFSSL CONFIGURATION] 
 * Centralized settings for wolfCrypt without heap and optimized for AVR.
 */

/* [SIL-2] No dynamic memory allocation */
#define WOLFSSL_STATIC_MEMORY
#define WOLFSSL_NO_MALLOC
#define WOLFSSL_MALLOC_CHECK

/* [AVR] Optimization - DISABLED for Host Tests if not on AVR */
#if defined(ARDUINO_ARCH_AVR)
#define WOLFSSL_AVR
#define USE_SLOW_SHA256
#define WOLFSSL_SMALL_STACK
#endif

/* [PROTOCOL] Required primitives only */
#define WOLFCRYPT_ONLY
#define NO_AES
#define NO_RSA
#define NO_DSA
#define NO_DH
#define NO_PWDBASED
#define NO_DES3
#define NO_MD5
#define NO_RC4
#define NO_ASN
#define NO_CODING
#define NO_FILESYSTEM
#define NO_SIG_WRAPPER
#define NO_OLD_TLS

/* [FEATURES] SHA-256, HMAC and HKDF */
#define WOLFSSL_SHA256
#define WOLFSSL_HMAC
#ifndef HAVE_HKDF
#define HAVE_HKDF
#endif
#ifndef WOLFSSL_HKDF
#define WOLFSSL_HKDF
#endif

/* Explicitly disable other hashes */
#define NO_SHA
#define NO_MD4
#define NO_MD2

/* [SECURITY] Hardening */
#define WOLFSSL_FORCE_ZERO
#define WOLFSSL_NO_FLOAT
#define NO_WRITEV
#define NO_MAIN_DRIVER