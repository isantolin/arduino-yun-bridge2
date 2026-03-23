#ifndef WOLFSSL_USER_SETTINGS_H
#define WOLFSSL_USER_SETTINGS_H

/* [AVR] Suprimir warnings benignos en código de terceros */
#if defined(ARDUINO_ARCH_AVR)
    #pragma GCC diagnostic ignored "-Wshift-count-overflow"
    #pragma GCC diagnostic ignored "-Woverflow"
#endif

#include <stddef.h>
#include <stdint.h>
/* Incluimos el tiempo del sistema PRIMERO */
#include <time.h>

/* ========================================================= */
/* McuBridge SIL-2 WolfSSL Configuration                     */
/* ========================================================= */

#define WOLFSSL_ARDUINO
#define SINGLE_THREADED
#define WC_NO_HARDEN

/* [TIME] Evitar redefinición de struct tm y time_t */
#define NO_ASN_TIME
#define USER_TIME
#define HAVE_TIME_H
#define HAVE_TIME_T_TYPE
#define HAVE_TM_TYPE
#define WOLFSSL_GMTIME
#define WOLFSSL_USE_TIME_H

/* Guardas internas de WolfSSL para forzar el salto de definiciones */
#define WOLFSSL_TM_STRUCT_DEFINED
#define WOLFSSL_GMTIME_STRUCT_DEFINED
#define _TM_DEFINED

/* Mapeo de funciones de tiempo requeridas */
#define XTIME wolfssl_time
#define XGMTIME wolfssl_gmtime

/* [AVR] Forzar tamaños de tipos para evitar warnings */
#if defined(ARDUINO_ARCH_AVR)
    #define SIZEOF_LONG 4
    #define SIZEOF_LONG_LONG 8
    #define WOLFSSL_IAR_ARM_AVR
    #define NO_64BIT
#endif

/* [SIL-2] No dynamic memory allocation */
#define WOLFSSL_STATIC_MEMORY
#define WOLFSSL_NO_MALLOC
#define WOLFSSL_MALLOC_CHECK

#if defined(ARDUINO_ARCH_AVR)
#define USE_SLOW_SHA256
#endif

/* [PROTOCOL] Required primitives only */
#define WOLFCRYPT_ONLY
#define NO_CERTS
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

#define NO_SHA
#define NO_MD4
#define NO_MD2

/* [SECURITY] Hardening */
#define WOLFSSL_FORCE_ZERO
#define WOLFSSL_NO_FLOAT
#define NO_WRITEV
#define NO_MAIN_DRIVER

#endif /* WOLFSSL_USER_SETTINGS_H */
