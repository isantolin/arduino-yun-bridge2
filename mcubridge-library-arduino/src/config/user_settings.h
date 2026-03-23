#ifndef MCUBRIDGE_WOLFSSL_CONFIG_H
#define MCUBRIDGE_WOLFSSL_CONFIG_H

/* [AVR] Suprimir warnings benignos en código de terceros */
#if defined(ARDUINO_ARCH_AVR) || defined(__AVR__)
    #pragma GCC diagnostic ignored "-Wshift-count-overflow"
    #pragma GCC diagnostic ignored "-Woverflow"
#endif

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
#define WOLFSSL_TM_STRUCT_DEFINED
#define WOLFSSL_GMTIME_STRUCT_DEFINED
#define _TM_DEFINED

#define XTIME wolfssl_time
#define XGMTIME wolfssl_gmtime

#if defined(__AVR__) || defined(ARDUINO_ARCH_AVR)
    #define SIZEOF_LONG 4
    #define SIZEOF_LONG_LONG 8
    #define WOLFSSL_IAR_ARM_AVR
    #define NO_64BIT
    #ifdef TIME_OVERRIDES
        #undef TIME_OVERRIDES
    #endif
#endif

#define WOLFSSL_STATIC_MEMORY
#define WOLFSSL_NO_MALLOC
#define WOLFSSL_MALLOC_CHECK

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

#define WOLFSSL_FORCE_ZERO
#define WOLFSSL_NO_FLOAT
#define NO_WRITEV
#define NO_MAIN_DRIVER

#endif /* MCUBRIDGE_WOLFSSL_CONFIG_H */
