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
        _stream->write(encoded, n);
        _stream->write(static_cast<uint8_t>(0));
        _stream->flush(); 
        return n + 1;
    }

private:
    static size_t encode(const uint8_t* input, size_t length, uint8_t* output) {
        size_t read_index = 0;
        size_t write_index = 1;
        size_t code_index = 0;
        uint8_t code = 1;

        while (read_index < length) {
            if (input[read_index] == 0) {
                output[code_index] = code;
                code = 1;
                code_index = write_index++;
                read_index++;
            } else {
                output[write_index++] = input[read_index++];
                code++;
                if (code == 0xFF) {
                    output[code_index] = code;
                    code = 1;
                    code_index = write_index++;
                }
            }
        }
        output[code_index] = code;
        return write_index;
    }

    static size_t decode(const uint8_t* input, size_t length, uint8_t* output) {
        size_t read_index = 0;
        size_t write_index = 0;
        while (read_index < length) {
            uint8_t code = input[read_index++];
            for (uint8_t i = 1; i < code; i++) {
                if (read_index >= length) return write_index;
                output[write_index++] = input[read_index++];
            }
            if (code < 0xFF && read_index < length) {
                output[write_index++] = 0;
            }
        }
        return write_index;
    }

    Stream* _stream;
    PacketHandler _handler;
    uint8_t _buffer[1024];
    size_t _read_index;
};
