/*
 * This file is part of Arduino MCU Ecosystem v2.
 */
#include "Bridge.h"
#include <etl/error_handler.h>

namespace etl {
    // [SIL-2] Minimal error handler for ETL when exceptions are disabled.
    // The library expects this function when ETL_THROW_EXCEPTIONS=0 is used.
    void exception_handler(const etl::exception& e) {
        // Log or handle error if necessary. In SIL-2, we prefer deterministic 
        // behavior over crashing.
        (void)e; // Prevent unused parameter warning
    }
}

// [SIL-2] Explicitly include Arduino.h to satisfy IntelliSense and ensure
// noInterrupts()/interrupts() are available in all compilation contexts.
#include <Arduino.h>
#include <TaskScheduler.h>

// --- [SAFETY GUARD START] ---
// CRITICAL: Prevent accidental STL usage on ALL architectures (memory fragmentation risk)
// SIL 2 Requirement: Dynamic allocation via STL containers is forbidden globally.
// ETL is allowed as it is static.
#ifndef BRIDGE_HOST_TEST
#if defined(_GLIBCXX_VECTOR) || defined(_GLIBCXX_STRING) || defined(_GLIBCXX_MAP)
  #error "CRITICAL: STL detected. Use ETL or standard arrays/pointers only to prevent heap fragmentation (SIL 2 Violation)."
#endif
#endif
// --- [SAFETY GUARD END] ---

BridgeClass::BridgeClass(Stream& stream) 
    : _transport(stream), 
      _authenticated(false),
      _last_received_seq(0),
      _awaiting_ack(false),
      _last_tx_time(0),
      _scheduler(nullptr),
      _serialTask(nullptr),
      _watchdogTask(nullptr),
      _begun(false) {
}

BridgeClass::BridgeClass(HardwareSerial& serial) 
    : _transport(serial), 
      _authenticated(false),
      _last_received_seq(0),
      _awaiting_ack(false),
      _last_tx_time(0),
      _scheduler(nullptr),
      _serialTask(nullptr),
      _watchdogTask(nullptr),
      _begun(false) {
}

void BridgeClass::begin(unsigned long baud, const char* secret, size_t secret_len) {
    if (secret && secret_len > 0) {
        _shared_secret.assign(secret, secret_len);
    }
    
    // Setup TaskScheduler
    static Scheduler scheduler;
    static Task serialTask(0, TASK_FOREVER, &BridgeClass::_serialTaskCallback);
    static Task watchdogTask(100, TASK_FOREVER, &BridgeClass::_watchdogTaskCallback);
    
    _scheduler = &scheduler;
    _serialTask = &serialTask;
    _watchdogTask = &watchdogTask;
    
    _serialTask->enable();
    _watchdogTask->enable();
    
    _begun = true;
}

void BridgeClass::process() {
    if (_scheduler) {
        _scheduler->execute();
    }
}

// Minimal implementations for remaining methods to satisfy linker
void BridgeClass::_serialTaskCallback() {}
void BridgeClass::_watchdogTaskCallback() {}
size_t BridgeClass::consoleWrite(uint8_t c) { (void)c; return 0; }
size_t BridgeClass::consoleWrite(const uint8_t* buffer, size_t size) { (void)buffer; (void)size; return 0; }
int BridgeClass::consoleRead() { return -1; }
int BridgeClass::consoleAvailable() { return 0; }
int BridgeClass::consolePeek() { return -1; }
void BridgeClass::consoleFlush() {}
void BridgeClass::datastorePut(const char* key, const char* value) { (void)key; (void)value; }
void BridgeClass::datastoreGet(const char* key, char* value, size_t max_len) { (void)key; (void)value; (void)max_len; }

// Global instance
#if BRIDGE_USE_USB_SERIAL
BridgeClass Bridge(Serial);
#else
BridgeClass Bridge(Serial1);
#endif
