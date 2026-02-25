/**
 * @file logging.h
 * @brief Standardized binary logging for Arduino MCU Bridge v2.
 */
#ifndef BRIDGE_LOGGING_H
#define BRIDGE_LOGGING_H

#include <Arduino.h>
#include <stdint.h>

namespace bridge {
namespace logging {

/**
 * @brief Logs binary data in standardized [DE AD BE EF] format to a stream.
 */
inline void log_hex(Print& stream, const uint8_t* data, size_t len) {
    if (!data || len == 0) {
        stream.print(F("[]"));
        return;
    }
    
    stream.print('[');
    for (size_t i = 0; i < len; ++i) {
        if (data[i] < 0x10) stream.print('0');
        stream.print(data[i], HEX);
        if (i < len - 1) stream.print(' ');
    }
    stream.print(']');
}

/**
 * @brief Logs a directional traffic event.
 */
inline void log_traffic(Print& stream, const char* direction, const char* label, const uint8_t* data, size_t len) {
    stream.print(direction);
    stream.print(' ');
    stream.print(label);
    stream.print(F(": "));
    log_hex(stream, data, len);
    stream.println();
}

} // namespace logging
} // namespace bridge

#endif // BRIDGE_LOGGING_H
