#pragma once

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>

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
    return write(reinterpret_cast<const std::uint8_t*>(str), std::strlen(str));
  }

  std::size_t print(const char* str) { return write(str); }

  std::size_t print(int value) {
    char buf[16];
    int len = std::snprintf(buf, sizeof(buf), "%d", value);
    if (len <= 0) return 0;
    return write(reinterpret_cast<const std::uint8_t*>(buf),
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
