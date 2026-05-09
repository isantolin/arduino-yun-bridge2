#ifndef __ETL_PROFILE_H__
#define __ETL_PROFILE_H__

// [SIL-2] MCU Bridge v2 - ETL Deterministic Profile
// This profile ensures no heap usage, no exceptions, and no RTTI.

#define ETL_NO_EXCEPTIONS
#ifndef ETL_NO_STL
#define ETL_NO_STL
#endif
#define ETL_STL_NOT_AVAILABLE
#define ETL_NO_RTTI
#define ETL_LOG_ERRORS
#undef ETL_VERBOSE_ERRORS
#define ETL_CHECK_PUSH_POP
#define ETL_CALLBACK_ON_ERROR

// [SIL-2] Global Compatibility Workarounds
#if defined(__AVR__)
  // 1. Resolve 'round' macro collision between Arduino.h and ETL C++17
  #if defined(round)
    #undef round
  #endif
  // 2. Resolve mpack 1.1.0 C++ generic double overload issues on 8-bit targets
  #ifndef mpack_write_double
    #define mpack_write_double mpack_write_float
  #endif
#endif

// [MEMORY OPT] Disable CRC32 Lookup Table (saves ~1KB - 2KB RAM)
// Force calculation on the fly.
#define ETL_CRC32_USE_TABLE 0

// Detected compiler-specific optimizations
#if defined(__AVR__)
#define ETL_COMPILER_GCC
#define ETL_CPP17_SUPPORTED 1
#elif defined(ARDUINO_ARCH_ESP32) || defined(ARDUINO_ARCH_ESP8266)
#define ETL_COMPILER_GCC
#define ETL_CPP17_SUPPORTED 1
#else
#define ETL_COMPILER_GENERIC
#define ETL_CPP17_SUPPORTED 1
#endif

// [SIL-2] ETL Callback Timer locking for Arduino
#if defined(ARDUINO)
#define ETL_CALLBACK_TIMER_USE_INTERRUPT_LOCK
#define ETL_CALLBACK_TIMER_DISABLE_INTERRUPTS noInterrupts()
#define ETL_CALLBACK_TIMER_ENABLE_INTERRUPTS interrupts()
#else
// Host / Generic (Single-threaded test environment)
#define ETL_CALLBACK_TIMER_USE_INTERRUPT_LOCK
#define ETL_CALLBACK_TIMER_DISABLE_INTERRUPTS
#define ETL_CALLBACK_TIMER_ENABLE_INTERRUPTS
#endif

#endif
