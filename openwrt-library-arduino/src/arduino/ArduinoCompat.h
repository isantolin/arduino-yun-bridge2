#pragma once

#include <cstdint>
#include <cstddef>

#include "../../tools/arduino_stub/include/Arduino.h"
#include "PrintCompat.h"

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

