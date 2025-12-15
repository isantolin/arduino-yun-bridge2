#pragma once

#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <string> // Added for std::string
#include <algorithm> // For std::min, std::max

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
#ifndef min
#define min(a, b) ((a) < (b) ? (a) : (b))
#endif
#ifndef max
#define max(a, b) ((a) > (b) ? (a) : (b))
#endif
#define round(x) ((x) >= 0 ? (long)((x) + 0.5) : (long)((x) - 0.5))

// Stub functions
inline unsigned long millis() { return 0; }
inline void delay(unsigned long) {}
inline void pinMode(uint8_t, uint8_t) {}
inline void digitalWrite(uint8_t, uint8_t) {}
inline int digitalRead(uint8_t) { return LOW; }

// Helper class for string manipulation (minimal stub)
class String {
public:
    String(const char* s = "") : _data(s ? s : "") {}
    String(int v) : _data(std::to_string(v)) {}
    
    const char* c_str() const { return _data.c_str(); }
    size_t length() const { return _data.length(); }
    
    // Minimal operators needed for tests
    bool operator==(const String& other) const { return _data == other._data; }
    bool operator==(const char* other) const { return _data == (other ? other : ""); }
    
private:
    std::string _data;
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
};

class Stream : public Print {
public:
    virtual int available() = 0;
    virtual int read() = 0;
    virtual int peek() = 0;
    virtual void flush() = 0;
};

// HardwareSerial stub
class HardwareSerial : public Stream {
public:
    void begin(unsigned long) {}
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
#define bitRead(value, bit) (((value) >> (bit)) & 0x01)
#define bitSet(value, bit) ((value) |= (1UL << (bit)))
#define bitClear(value, bit) ((value) &= ~(1UL << (bit)))
#define bitWrite(value, bit, bitvalue) (bitvalue ? bitSet(value, bit) : bitClear(value, bit))