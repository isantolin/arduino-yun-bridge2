/**
 * @file logging.h
 * @brief Standardized binary logging for Arduino MCU Bridge v2.
 * [SIL-2] Uses standardized [DE AD BE EF] format and avoids blocking IO.
 */
#ifndef BRIDGE_LOGGING_H
#define BRIDGE_LOGGING_H

#include <Arduino.h>
#include <stdint.h>
#include <etl/span.h>
#include <etl/algorithm.h>

namespace bridge {
namespace logging {

/**
 * @brief Logs binary data in standardized [DE AD BE EF] format to a stream.
 */
inline void log_hex(Print& stream, etl::span<const uint8_t> data) {
  if (data.empty()) {
    stream.print(F("[]"));
    return;
  }

  stream.print('[');
  // [SIL-2] Use ETL for-each algorithm with C++14 generic lambda
  size_t i = 0;
  etl::for_each(data.begin(), data.end(), [&stream, &i, size = data.size()](auto byte) {
    if (byte < 0x10) stream.print('0');
    stream.print(byte, HEX);
    if (++i < size) stream.print(' ');
  });
  stream.print(']');
}

/**
 * @brief Logs a directional traffic event.
 * [SIL-2] Standardized directional labels for automated log parsing.
 */
inline void log_traffic(Print& stream, const char* direction, const char* label,
                        etl::span<const uint8_t> data) {
  // Avoid recursion if stream is Console and we are logging Console traffic
  // However, Bridge typically logs to Serial, and Console sends via Bridge.
  stream.print(direction);
  stream.print(' ');
  stream.print(label);
  stream.print(F(": "));
  log_hex(stream, data);
  stream.println();
}

}  // namespace logging
}  // namespace bridge

#endif  // BRIDGE_LOGGING_H
