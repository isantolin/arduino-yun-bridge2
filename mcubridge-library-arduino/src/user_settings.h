#ifndef MCUBRIDGE_USER_SETTINGS_H
#define MCUBRIDGE_USER_SETTINGS_H

/* [CRITICAL] This file is used by the official wolfSSL Arduino library */
/* when WOLFSSL_USER_SETTINGS is defined. */

#include <stddef.h>
#include <stdint.h>
#include <time.h>

#define WOLFSSL_ARDUINO
#define WOLFCRYPT_ONLY
#define SINGLE_THREADED
#define WC_NO_HARDEN
#define WOLFSSL_USER_IO
#define WOLFSSL_API
#define USE_SLOW_SHA256

/* Let wolfSSL handle types using architecture hints */
#if defined(__AVR__) || defined(ARDUINO_ARCH_AVR)
    #define WC_16BIT_CPU
    #define SIZEOF_INT 2
    #define SIZEOF_SHORT 2
    #define SIZEOF_LONG 4
    #define SIZEOF_LONG_LONG 8
#endif

#if defined(__AVR__) || defined(ARDUINO_ARCH_AVR)
    #define NO_64BIT
    #undef TIME_OVERRIDES
#endif

/* Features enabled for McuBridge */
#define WOLFSSL_SHA256
#define WOLFSSL_HMAC
#define HAVE_HKDF
#define WOLFSSL_HKDF

/* Protocol protections */
#define NO_AES
#define NO_RSA
#define NO_DSA
#define NO_DH
#define NO_PWDBASED
#define NO_DES3
#define NO_MD5
#define NO_MD4
#define NO_SHA
#define NO_RC4
#define NO_ASN
#define NO_FILESYSTEM
#define NO_MAIN_DRIVER

/* Time configuration */
#define USER_TIME
#define XTIME wolfssl_time
#define XGMTIME wolfssl_gmtime
#define WOLFSSL_GMTIME
#define HAVE_GMTIME_R
#define HAVE_TIME_H
#define HAVE_TIME_T_TYPE
#define HAVE_TM_TYPE

#endif /* MCUBRIDGE_USER_SETTINGS_H */
