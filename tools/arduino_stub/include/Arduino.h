#pragma once

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// Bring standard C fixed-width types into global namespace for Arduino compatibility
#ifdef __cplusplus
using ::int16_t;
using ::int32_t;
using ::int8_t;
using ::size_t;
using ::uint16_t;
using ::uint32_t;
using ::uint8_t;
#endif

// Basic Arduino types
using boolean = bool;
using byte = uint8_t;
using word = uint16_t;

#ifndef _NEW
#define _NEW
inline void* operator new(size_t, void* ptr) noexcept { return ptr; }
inline void operator delete(void*, void*) noexcept {}
#endif

// Constants
#define HIGH 1
#define LOW 0
#define INPUT 0
#define OUTPUT 1
#define INPUT_PULLUP 2
#define LED_BUILTIN 13
// Host tests use the Uno core's pin-count contract.
#define NUM_DIGITAL_PINS 20
#define NUM_ANALOG_INPUTS 6

// Print bases
#define BIN 2
#define OCT 8
#define DEC 10
#define HEX 16

template <typename T>
inline T abs(T x) {
  return (x > 0) ? x : -x;
}

#undef min
#undef max

template <typename T>
inline T min(T a, T b) {
  return (a < b) ? a : b;
}

template <typename T>
inline T max(T a, T b) {
  return (a > b) ? a : b;
}

// Replace round macro with a template to avoid conflict with cmath
template <typename T>
inline long round(T x) {
  return (x >= 0) ? static_cast<long>(x + 0.5) : static_cast<long>(x - 0.5);
}

#ifdef ARDUINO_STUB_CUSTOM_MILLIS
unsigned long millis();
unsigned long micros();
void delay(unsigned long);
#else
inline unsigned long millis() { return 0; }
inline unsigned long micros() { return 0; }
inline void delay(unsigned long) {}
#endif

inline void delayMicroseconds(unsigned int /*us*/) {}
inline void yield() {}
inline void pinMode(uint8_t, uint8_t) {}
inline void digitalWrite(uint8_t, uint8_t) {}
inline int digitalRead(uint8_t) { return LOW; }

inline void analogWrite(uint8_t, int) {}
inline int analogRead(uint8_t) { return 0; }

class String {
 public:
  static constexpr size_t kCapacity = 64;

  String(const char* s = "") { assign(s); }

  String(int v) {
    char buf[16];
    (void)snprintf(buf, sizeof(buf), "%d", v);
    assign(buf);
  }

  const char* c_str() const { return data_; }
  size_t length() const { return length_; }

  bool concat(const char* s) {
    if (!s) return true;
    size_t slen = strlen(s);
    if (length_ + slen < kCapacity) {
      memcpy(data_ + length_, s, slen);
      length_ += slen;
      data_[length_] = '\0';
      return true;
    }
    return false;
  }

  bool operator==(const String& other) const {
    return strcmp(data_, other.data_) == 0;
  }

  bool operator==(const char* other) const {
    return strcmp(data_, (other ? other : "")) == 0;
  }

 private:
  void assign(const char* s) {
    const char* src = s ? s : "";
    size_t slen = strlen(src);
    if (slen >= kCapacity) {
      slen = kCapacity - 1;
    }
    memcpy(data_, src, slen);
    data_[slen] = '\0';
    length_ = slen;
  }

  char data_[kCapacity] = {};
  size_t length_ = 0;
};

class __FlashStringHelper;
#define F(str) (reinterpret_cast<const __FlashStringHelper*>(str))

#define PROGMEM
#define PSTR(s) (s)
#define pgm_read_byte(p) (*reinterpret_cast<const uint8_t*>(p))
#define pgm_read_word(p) (*reinterpret_cast<const uint16_t*>(p))

inline size_t strnlen_P(const char* s, size_t maxlen) {
  const char* end = static_cast<const char*>(memchr(s, '\0', maxlen));
  return (end == nullptr) ? maxlen : static_cast<size_t>(end - s);
}

inline void* memcpy_P(void* dest, const void* src, size_t n) {
  return memcpy(dest, src, n);
}

class Print;

class Printable {
 public:
  virtual ~Printable() = default;
  virtual size_t printTo(Print& p) const = 0;
};

class Print {
 public:
  virtual ~Print() = default;

  virtual size_t write(uint8_t) = 0;
  virtual size_t write(const uint8_t* buffer, size_t size) {
    size_t n = 0;
    while (size--) {
      if (write(*buffer++))
        n++;
      else
        break;
    }
    return n;
  }

  size_t print(const char[]) { return 0; }
  size_t print(char) { return 0; }
  size_t print(int, int = 10) { return 0; }
  size_t println(const char[]) { return 0; }
  size_t println(int, int = 10) { return 0; }
  size_t println(void) { return 0; }
  size_t print(const __FlashStringHelper*) { return 0; }
  size_t println(const __FlashStringHelper*) { return 0; }
};

class Stream : public Print {
 public:
  virtual ~Stream() = default;

  virtual int available() = 0;
  virtual int read() = 0;
  virtual int peek() = 0;
  virtual void flush() = 0;
  virtual void setTimeout(unsigned long) {}

  size_t readBytes(char* buffer, size_t length) {
    size_t count = 0;
    while (count < length) {
      int c = read();
      if (c < 0) break;
      *buffer++ = static_cast<char>(c);
      count++;
    }
    return count;
  }

  size_t readBytes(uint8_t* buffer, size_t length) {
    return readBytes(reinterpret_cast<char*>(buffer), length);
  }
};

extern Stream* g_arduino_stream_delegate;

class HardwareSerial : public Stream {
 public:
  void begin(unsigned long) {}
  void end() {}

  using Print::write;

  size_t write(uint8_t c) override {
    return g_arduino_stream_delegate ? g_arduino_stream_delegate->write(c) : 1;
  }
  int available() override {
    return g_arduino_stream_delegate ? g_arduino_stream_delegate->available() : 0;
  }
  int read() override {
    return g_arduino_stream_delegate ? g_arduino_stream_delegate->read() : -1;
  }
  int peek() override {
    return g_arduino_stream_delegate ? g_arduino_stream_delegate->peek() : -1;
  }
  void flush() override {
    if (g_arduino_stream_delegate) g_arduino_stream_delegate->flush();
  }
};

extern HardwareSerial Serial;
extern HardwareSerial Serial1;

template <typename T>
constexpr T constrain(T value, T minimum, T maximum) {
  return (value < minimum) ? minimum : ((value > maximum) ? maximum : value);
}

#define bitRead(value, bit) (((value) >> (bit)) & 1)
#define bitSet(value, bit) ((value) |= (1UL << (bit)))
#define bitClear(value, bit) ((value) &= ~(1UL << (bit)))
#define bitWrite(value, bit, bitvalue) \
  (bitvalue ? bitSet(value, bit) : bitClear(value, bit))

inline void noInterrupts() {}
inline void interrupts() {}
