#pragma once

#include <cstdint>
#include <cstddef>
#include <cstring>

using byte = std::uint8_t;
using boolean = bool;

using std::size_t;
using std::uint8_t;
using std::uint16_t;
using std::uint32_t;
using std::uint64_t;

#ifndef HIGH
#define HIGH 0x1
#endif

#ifndef LOW
#define LOW 0x0
#endif

#ifndef INPUT
#define INPUT 0x0
#endif

#ifndef OUTPUT
#define OUTPUT 0x1
#endif

#ifndef INPUT_PULLUP
#define INPUT_PULLUP 0x2
#endif

#ifndef PROGMEM
#define PROGMEM
#endif

#ifndef F
#define F(x) (x)
#endif

#ifndef pgm_read_byte
#define pgm_read_byte(addr) (*(addr))
#endif

inline unsigned long millis() { return 0; }
inline void delay(unsigned long) {}
inline void delayMicroseconds(unsigned int) {}
