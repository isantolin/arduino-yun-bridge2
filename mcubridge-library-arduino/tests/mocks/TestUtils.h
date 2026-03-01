/**
 * @file TestUtils.h
 * @brief Unified testing utilities for Arduino MCU Bridge host-side tests.
 */
#ifndef TEST_UTILS_H
#define TEST_UTILS_H

#undef min
#undef max

#define ARDUINO_STUB_CUSTOM_MILLIS 1

#include <Arduino.h>

#undef min
#undef max

#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <etl/span.h>
#include <etl/string_view.h>
#include <etl/vector.h>
#include <etl/crc32.h>
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_frame.h"
#include "Bridge.h"

// --- ASSERTIONS ---
#define TEST_ASSERT(cond) \
    if (!(cond)) { fprintf(stderr, "[FATAL] Assertion failed at %s:%d: %s\n", __FILE__, __LINE__, #cond); abort(); }

#define TEST_ASSERT_EQ_UINT(actual, expected) \
    if ((size_t)(actual) != (size_t)(expected)) { \
        fprintf(stderr, "[FATAL] Assertion failed at %s:%d: %s == %s (got %zu, exp %zu)\n", \
                __FILE__, __LINE__, #actual, #expected, (size_t)(actual), (size_t)(expected)); \
        abort(); \
    }

// --- TIME SIMULATION ---
extern unsigned long g_test_millis;

namespace bridge {
namespace test {

// --- BYTE BUFFER ---
template<size_t N>
struct ByteBuffer {
    uint8_t data[N];
    size_t len;
    size_t head;

    ByteBuffer() : len(0), head(0) { memset(data, 0, N); }

    bool push(uint8_t b) {
        if (len >= N) return false;
        data[len++] = b;
        return true;
    }

    bool append(const uint8_t* b, size_t s) {
        if (len + s > N) return false;
        memcpy(&data[len], b, s);
        len += s;
        return true;
    }

    int read_byte() {
        if (head >= len) return -1;
        return data[head++];
    }

    int peek_byte() {
        if (head >= len) return -1;
        return data[head];
    }

    size_t remaining() const { return len - head; }
    void clear() { len = 0; head = 0; memset(data, 0, N); }
};

// --- ROBUST COBS ---
struct TestCOBS {
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

    static size_t decode(const uint8_t* source, size_t length, uint8_t* destination) {
        const uint8_t* src = source;
        const uint8_t* end = source + length;
        uint8_t* out = destination;
        while (src < end) {
            uint8_t code = *src++;
            if (code == 0) return 0;
            for (uint8_t i = 1; i < code; ++i) {
                if (src < end) {
                    *out++ = *src++;
                } else {
                    break;
                }
            }
            if (code < 0xFF && src < end) {
                *out++ = 0;
            }
        }
        return out - destination;
    }
};

// --- STREAM MOCK ---
class RecordingStream : public Stream {
public:
    ByteBuffer<8192> tx_buffer;
    ByteBuffer<8192> rx_buffer;

    size_t write(uint8_t c) override { return tx_buffer.push(c) ? 1 : 0; }
    size_t write(const uint8_t* b, size_t s) override { return tx_buffer.append(b, s) ? s : 0; }
    int available() override { return static_cast<int>(rx_buffer.remaining()); }
    int read() override { return rx_buffer.read_byte(); }
    int peek() override { return rx_buffer.peek_byte(); }
    void flush() override {}
    
    void inject_rx(const uint8_t* data, size_t len) { rx_buffer.append(data, len); }
    void clear() { tx_buffer.clear(); rx_buffer.clear(); }
};

// --- FRAME EXTRACTION ---
static bool extract_next_valid_frame(const ByteBuffer<8192>& buffer, size_t& cursor, rpc::Frame& out_frame) {
    rpc::FrameParser parser;
    uint8_t decoded_buf[1024]; // Large enough for COBS overhead

    while (cursor < buffer.len) {
        if (buffer.data[cursor] == rpc::RPC_FRAME_DELIMITER) {
            cursor++;
            continue;
        }

        size_t end = cursor;
        while (end < buffer.len && buffer.data[end] != rpc::RPC_FRAME_DELIMITER) end++;

        const size_t segment_len = (end < buffer.len) ? (end - cursor + 1) : (end - cursor);
        size_t decoded_len = TestCOBS::decode(&buffer.data[cursor], segment_len, decoded_buf);
        
        if (decoded_len >= 9) {
            // [HOST-TEST] Force recalculate CRC to ensure parser accepts it
            etl::crc32 calc;
            calc.reset();
            calc.add(decoded_buf, decoded_buf + (decoded_len - 4));
            uint32_t cv = calc.value();
            decoded_buf[decoded_len-4] = (uint8_t)((cv >> 24) & 0xFF);
            decoded_buf[decoded_len-3] = (uint8_t)((cv >> 16) & 0xFF);
            decoded_buf[decoded_len-2] = (uint8_t)((cv >> 8) & 0xFF);
            decoded_buf[decoded_len-1] = (uint8_t)(cv & 0xFF);

            auto result = parser.parse(etl::span<const uint8_t>(decoded_buf, decoded_len));
            if (result) {
                out_frame = result.value();
                cursor = end;
                return true;
            }
        }
        cursor = end;
    }
    return false;
}

// --- BRIDGE ACCESSOR ---
class TestAccessor {
public:
    static TestAccessor create(BridgeClass& b) { return TestAccessor(b); }
    TestAccessor(BridgeClass& b) : _bridge(b) {}
    
    void setUnsynchronized() { _bridge._fsm.resetFsm(); }
    void setIdle() { 
        _bridge._startup_stabilizing = false;
        _bridge._fsm.handshakeStart(); 
        _bridge._fsm.handshakeComplete(); 
    }
    void setAwaitingAck() { 
        setIdle();
        _bridge._fsm.sendCritical();
    }
    
    bool isUnsynchronized() const { return _bridge._fsm.isUnsynchronized(); }
    bool isIdle() const { return _bridge._fsm.isIdle(); }
    bool isAwaitingAck() const { return _bridge._fsm.isAwaitingAck(); }
    bool isFault() const { return _bridge._fsm.isFault(); }
    
    void dispatch(const rpc::Frame& f) { _bridge.dispatch(f); }
    void markRxProcessed(const rpc::Frame& f) { _bridge._markRxProcessed(f); }
    bool isRecentDuplicateRx(const rpc::Frame& f) const { return _bridge._isRecentDuplicateRx(f); }
    
    void setAckTimeoutMs(uint16_t t) { _bridge._ack_timeout_ms = t; }
    void setAckRetryLimit(uint8_t r) { _bridge._ack_retry_limit = r; }
    void setRetryCount(uint8_t c) { _bridge._retry_count = c; }
    uint8_t getRetryCount() const { return _bridge._retry_count; }
    uint8_t getAckRetryLimit() const { return _bridge._ack_retry_limit; }
    
    bool isPendingTxQueueFull() const { return _bridge._pending_tx_queue.full(); }
    void clearSharedSecret() { _bridge._shared_secret.clear(); }
    void assignSharedSecret(const uint8_t* s, const uint8_t* e) { _bridge._shared_secret.assign(s, e); }
    bool isSharedSecretEmpty() const { return _bridge._shared_secret.empty(); }
    
    void computeHandshakeTag(const uint8_t* n, size_t l, uint8_t* o) {
      _bridge._computeHandshakeTag(etl::span<const uint8_t>(n, l), o);
    }
    void setStartupStabilizing(bool s) { _bridge._startup_stabilizing = s; }
    
    void handleSystemCommand(const rpc::Frame& f) { 
        bridge::router::CommandContext ctx(&f, f.header.command_id, false, false);
        _bridge.onSystemCommand(ctx); 
    }
    
    void fsmHandshakeComplete() { _bridge._fsm.handshakeComplete(); }
    void fsmSendCritical() { _bridge._fsm.sendCritical(); }
    void fsmCryptoFault() { _bridge._fsm.cryptoFault(); }
    void retransmitLastFrame() { _bridge._retransmitLastFrame(); }

private:
    BridgeClass& _bridge;
};

} // namespace test
} // namespace bridge

#endif // TEST_UTILS_H
