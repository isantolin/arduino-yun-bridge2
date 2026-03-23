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

// [MEMORY OPT] Disable CRC32 Lookup Table (saves ~1KB - 2KB RAM)
// Force calculation on the fly.
#define ETL_CRC32_USE_TABLE 0

// Detected compiler-specific optimizations
// NOTE: We compile with -std=c++14 for language features (digit separators,
// using aliases), but ETL must stay at CPP11 level because avr-gcc 5.4
// (arduino:avr 1.8.7) lacks full relaxed-constexpr support that ETL
// requires when ETL_CPP14_SUPPORTED is set.
#if defined(__AVR__)
#define ETL_COMPILER_GCC
#define ETL_CPP11_SUPPORTED 1
// [SIL-2 GUARD] avr-gcc 5.4 lacks relaxed-constexpr; ETL_CPP14 breaks build.
#if defined(ETL_CPP14_SUPPORTED)
#error "ETL_CPP14_SUPPORTED must NOT be set for AVR targets (avr-gcc 5.4 incompatible)"
#endif
#elif defined(ARDUINO_ARCH_ESP32) || defined(ARDUINO_ARCH_ESP8266)
#define ETL_COMPILER_GCC
#define ETL_CPP11_SUPPORTED 1
#else
#define ETL_COMPILER_GENERIC
#define ETL_CPP11_SUPPORTED 1
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
