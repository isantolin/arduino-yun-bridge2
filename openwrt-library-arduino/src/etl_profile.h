#ifndef __ETL_PROFILE_H__
#define __ETL_PROFILE_H__

// [SIL-2] MCU Bridge v2 - ETL Deterministic Profile
// This profile ensures no heap usage, no exceptions, and no RTTI.

#define ETL_NO_EXCEPTIONS
#define ETL_NO_STL
#define ETL_STL_NOT_AVAILABLE
#define ETL_NO_RTTI
#define ETL_LOG_ERRORS
#define ETL_VERBOSE_ERRORS
#define ETL_CHECK_PUSH_POP
#define ETL_CALLBACK_ON_ERROR

// Detected compiler-specific optimizations
#if defined(__AVR__)
  #define ETL_COMPILER_GCC
  #define ETL_CPP11_SUPPORTED 1
#elif defined(ARDUINO_ARCH_ESP32) || defined(ARDUINO_ARCH_ESP8266)
  #define ETL_COMPILER_GCC
  #define ETL_CPP11_SUPPORTED 1
#else
  #define ETL_COMPILER_GENERIC
  #define ETL_CPP11_SUPPORTED 1
#endif

#endif
