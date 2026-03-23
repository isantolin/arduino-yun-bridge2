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

/* Force explicit types to resolve LTO mismatches */
#include <stdint.h>
#if defined(__AVR__) || defined(ARDUINO_ARCH_AVR)
    #define WC_16BIT_CPU
    typedef unsigned char  byte;
    typedef unsigned int   word16;
    typedef unsigned long  word32;
#else
    typedef uint8_t  byte;
    typedef uint16_t word16;
    typedef uint32_t word32;
#endif
#define WOLFSSL_TYPES_H

#if defined(__AVR__) || defined(ARDUINO_ARCH_AVR)
    #define SIZEOF_LONG 4
    #define SIZEOF_LONG_LONG 8
    #define WOLFSSL_IAR_ARM_AVR
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
