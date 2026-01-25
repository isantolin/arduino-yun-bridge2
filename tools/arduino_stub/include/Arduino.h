#pragma once

#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <cstdio>

// Basic types
using boolean = bool;
using byte = uint8_t;
using word = uint16_t;

// Constants
#define HIGH 1
#define LOW 0
#define INPUT 0
#define OUTPUT 1
#define INPUT_PULLUP 2
#define LED_BUILTIN 13

// Math macros
#define abs(x) ((x) > 0 ? (x) : -(x))
template <class T, class L>
auto min(T a, L b) -> decltype(a < b ? a : b) { return (a < b) ? a : b; }
template <class T, class L>
auto max(T a, L b) -> decltype(a > b ? a : b) { return (a > b) ? a : b; }
#define round(x) ((x) >= 0 ? (long)((x) + 0.5) : (long)((x) - 0.5))

// Stub functions
// Allow host tests to override timing behavior (e.g., time travel) by defining
// ARDUINO_STUB_CUSTOM_MILLIS before including Arduino headers.
#ifndef ARDUINO_STUB_CUSTOM_MILLIS
inline unsigned long millis() { return 0; }
inline void delay(unsigned long) {}
#endif
// Fix: Comment out unused parameter name to avoid compiler warning
inline void delayMicroseconds(unsigned int /*us*/) {} 
inline void yield() {} 
inline void pinMode(uint8_t, uint8_t) {}
inline void digitalWrite(uint8_t, uint8_t) {}
inline int digitalRead(uint8_t) { return LOW; }

// --- FIXED: Missing Analog Stubs ---
inline void analogWrite(uint8_t, int) {}
inline int analogRead(uint8_t) { return 0; }
// -----------------------------------

// Helper class for string manipulation (minimal stub)
class String {
public:
    static constexpr size_t kCapacity = 64;

    String(const char* s = "") { assign(s); }

    String(int v) {
        char buf[16];
        (void)::snprintf(buf, sizeof(buf), "%d", v);
        assign(buf);
    }

    const char* c_str() const { return data_; }
    size_t length() const { return length_; }

    bool operator==(const String& other) const {
        return ::strcmp(data_, other.data_) == 0;
    }

    bool operator==(const char* other) const {
        return ::strcmp(data_, (other ? other : "")) == 0;
    }

private:
    void assign(const char* s) {
        const char* src = s ? s : "";
        ::strncpy(data_, src, kCapacity - 1);
        data_[kCapacity - 1] = '\0';
        length_ = ::strlen(data_);
    }

    char data_[kCapacity] = {};
    size_t length_ = 0;
};

// F macro for Flash strings (no-op on host)
class __FlashStringHelper;
#define F(str) (reinterpret_cast<const __FlashStringHelper*>(str))

// PROGMEM macros (no-op on host)
#define PROGMEM
#define PSTR(s) (s)
#define pgm_read_byte(p) (*(const uint8_t*)(p))
#define pgm_read_word(p) (*(const uint16_t*)(p))

// Base classes needed for HardwareSerial
class Print {
public:
    virtual ~Print() = default; // Added virtual destructor for safety

    virtual size_t write(uint8_t) = 0;
    virtual size_t write(const uint8_t *buffer, size_t size) {
        size_t n = 0;
        while (size--) {
            if (write(*buffer++)) n++;
            else break;
        }
        return n;
    }
    // Stub print methods
    size_t print(const char[]) { return 0; }
    size_t print(char) { return 0; }
    size_t print(int, int = 10) { return 0; }
    size_t println(const char[]) { return 0; }
    size_t println(int, int = 10) { return 0; }
    size_t println(void) { return 0; }
    size_t print(const __FlashStringHelper *) { return 0; }
    size_t println(const __FlashStringHelper *) { return 0; }
};

class Stream : public Print {
public:
    virtual ~Stream() = default; // Added virtual destructor for safety

    virtual int available() = 0;
    virtual int read() = 0;
    virtual int peek() = 0;
    virtual void flush() = 0;
};

// HardwareSerial stub
class HardwareSerial : public Stream {
public:
    void begin(unsigned long) {}
    void end() {}
    
    // Fix: Unhide base class write(const uint8_t*, size_t)
    using Print::write;
    
    size_t write(uint8_t) override { return 1; }
    int available() override { return 0; }
    int read() override { return -1; }
    int peek() override { return -1; }
    void flush() override {}
};

extern HardwareSerial Serial;
extern HardwareSerial Serial1;

// C++11 compatible constexpr constrain
template <typename T>
constexpr T constrain(T value, T minimum, T maximum) {
    return (value < minimum) ? minimum : ((value > maximum) ? maximum : value);
}

// Bit manipulation macros
#define bitRead(value, bit) (((value) >> (bit)) & 1)
#define bitSet(value, bit) ((value) |= (1UL << (bit)))
#define bitClear(value, bit) ((value) &= ~(1UL << (bit)))
#define bitWrite(value, bit, bitvalue) (bitvalue ? bitSet(value, bit) : bitClear(value, bit))

// Interrupts (Stubs for host tests)
// In a host test environment, we generally run single-threaded logic tests,
// so disabling/enabling interrupts can be treated as no-ops.
// These are required because Bridge.cpp uses them for atomic state access.
inline void noInterrupts() {}
inline void interrupts() {}