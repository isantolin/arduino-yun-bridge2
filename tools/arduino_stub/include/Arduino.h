#pragma once

#include <cstdint>
#include <cstddef>
#include <cstdio>
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

class Print {
 public:
	virtual ~Print() = default;

	virtual std::size_t write(std::uint8_t c) = 0;

	virtual std::size_t write(const std::uint8_t* buffer, std::size_t size) {
		if (!buffer) return 0;
		std::size_t written = 0;
		while (written < size) {
			if (write(buffer[written]) == 0) break;
			++written;
		}
		return written;
	}

	std::size_t write(const char* str) {
		if (!str) return 0;
		return write(
				reinterpret_cast<const std::uint8_t*>(str),
				std::strlen(str));
	}

	std::size_t print(const char* str) { return write(str); }

	std::size_t print(int value) {
		char buf[16];
		int len = std::snprintf(buf, sizeof(buf), "%d", value);
		if (len <= 0) return 0;
		if (static_cast<std::size_t>(len) >= sizeof(buf)) {
			len = static_cast<int>(sizeof(buf) - 1);
		}
		return write(
				reinterpret_cast<const std::uint8_t*>(buf),
				static_cast<std::size_t>(len));
	}

	std::size_t println(const char* str) {
		std::size_t n = print(str);
		n += println();
		return n;
	}

	std::size_t println(int value) {
		std::size_t n = print(value);
		n += println();
		return n;
	}

	std::size_t println() {
		const std::uint8_t newline[2] = {'\r', '\n'};
		return write(newline, 2);
	}
};

class Stream : public Print {
 public:
	Stream() = default;
	~Stream() override = default;

	virtual int available() { return 0; }
	virtual int read() { return -1; }
	virtual int peek() { return -1; }
	virtual void flush() {}

	using Print::write;

	std::size_t write(std::uint8_t c) override {
		(void)c;
		return 1;
	}

	std::size_t write(const std::uint8_t* buffer, std::size_t size) override {
		if (!buffer) {
			return 0;
		}
		return Print::write(buffer, size);
	}
};

class HardwareSerial : public Stream {
 public:
	HardwareSerial() = default;

	void begin(unsigned long) {}
	void end() {}
	void setTimeout(unsigned long) {}
};

inline HardwareSerial Serial;
inline HardwareSerial Serial1;

template <typename T>
constexpr T constrain(T value, T minimum, T maximum) {
	if (value < minimum) {
		return minimum;
	}
	if (value > maximum) {
		return maximum;
	}
	return value;
}

inline void pinMode(uint8_t, uint8_t) {}
inline void digitalWrite(uint8_t, uint8_t) {}
inline int digitalRead(uint8_t) { return 0; }
inline void analogWrite(uint8_t, int) {}
inline int analogRead(uint8_t) { return 0; }
