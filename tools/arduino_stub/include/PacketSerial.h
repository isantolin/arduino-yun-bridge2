#pragma once
#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

class PacketSerial {
public:
    typedef void (*PacketHandler)(const uint8_t* buffer, size_t size);
    
    PacketSerial() : _stream(nullptr), _handler(nullptr), _read_index(0) {
        memset(_buffer, 0, sizeof(_buffer));
    }
    
    void setStream(Stream* stream) { _stream = stream; }
    void setPacketHandler(PacketHandler handler) { _handler = handler; }
    
    void update() {
        if (!_stream) return;
        while (_stream->available() > 0) {
            int b = _stream->read();
            if (b < 0) break;
            uint8_t val = static_cast<uint8_t>(b);
            if (val == 0) {
                if (_read_index > 0) {
                    uint8_t decoded[1024];
                    size_t n = decode(_buffer, _read_index, decoded);
                    if (n > 0 && _handler) _handler(decoded, n);
                }
                _read_index = 0;
            } else {
                if (_read_index < sizeof(_buffer)) _buffer[_read_index++] = val;
                else _read_index = 0;
            }
        }
    }
    
    size_t send(const uint8_t* buffer, size_t len) {
        if (!_stream || len == 0) return 0;
        uint8_t encoded[1024];
        size_t n = encode(buffer, len, encoded);
        size_t w = _stream->write(encoded, n);
        _stream->write(static_cast<uint8_t>(0));
        _stream->flush();
        return w + 1;
    }

private:
    static size_t encode(const uint8_t* src, size_t len, uint8_t* dst) {
        uint8_t* start = dst;
        uint8_t* code_ptr = dst++;
        uint8_t code = 1;
        for (size_t i = 0; i < len; ++i) {
            if (src[i] == 0) {
                *code_ptr = code;
                code_ptr = dst++;
                code = 1;
            } else {
                *dst++ = src[i];
                if (++code == 0xFF) {
                    *code_ptr = code;
                    code_ptr = dst++;
                    code = 1;
                }
            }
        }
        *code_ptr = code;
        return dst - start;
    }

    static size_t decode(const uint8_t* src, size_t len, uint8_t* dst) {
        const uint8_t* end = src + len;
        uint8_t* out = dst;
        while (src < end) {
            uint8_t code = *src++;
            if (code == 0) return 0;
            for (uint8_t i = 1; i < code; ++i) {
                if (src >= end) break;
                *out++ = *src++;
            }
            if (code < 0xFF && src < end) *out++ = 0;
        }
        return out - dst;
    }

    Stream* _stream;
    PacketHandler _handler;
    uint8_t _buffer[1024];
    size_t _read_index;
};
