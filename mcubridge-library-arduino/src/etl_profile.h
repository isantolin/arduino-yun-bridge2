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

// ETL 20.44+ controls CRC table size via type selection (crc32_t4/16/256),
// not ETL_CRC32_USE_TABLE. We intentionally keep etl::crc32 as project policy.

// Detected compiler-specific optimizations
#if defined(__AVR__)
#define ETL_COMPILER_GCC
#define ETL_CPP17_SUPPORTED 1
#define ETL_CRC_TABLE_PROGMEM
#define ETL_CRC_TABLE_PROGMEM
#elif defined(ARDUINO_ARCH_ESP32) || defined(ARDUINO_ARCH_ESP8266)
#define ETL_COMPILER_GCC
#define ETL_CPP17_SUPPORTED 1
#define ETL_CRC_TABLE_PROGMEM
#define ETL_CRC_TABLE_PROGMEM
#else
#define ETL_COMPILER_GENERIC
#define ETL_CPP17_SUPPORTED 1
#define ETL_CRC_TABLE_PROGMEM
#define ETL_CRC_TABLE_PROGMEM
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
