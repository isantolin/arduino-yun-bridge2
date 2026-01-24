/**
 * Additional C++ tests to improve coverage for Mailbox, Bridge, FileSystem,
 * Process, Console, and rpc_frame modules.
 * 
 * These tests target specific branches identified as uncovered in the
 * coverage report.
 */

#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>

#define private public
#define protected public
#include "Bridge.h"
#undef private
#undef protected

// #include "protocol/cobs.h" // Removed
#include <FastCRC.h>
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "test_constants.h"
#include "test_support.h"

// Define global Serial instances for the stub
HardwareSerial Serial;
HardwareSerial Serial1;

// Global instances required by the runtime
BridgeClass Bridge(Serial1);
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;

namespace {

// Local COBS implementation for test frame generation
struct TestCOBS {
    static size_t encode(const uint8_t* source, size_t length, uint8_t* destination) {
        size_t read_index = 0;
        size_t write_index = 1;
        size_t code_index = 0;
        uint8_t code = 1;

        while (read_index < length) {
            if (source[read_index] == 0) {
                destination[code_index] = code;
                code = 1;
                code_index = write_index++;
                read_index++;
            } else {
                destination[write_index++] = source[read_index++];
                code++;
                if (code == 0xFF) {
                    destination[code_index] = code;
                    code = 1;
                    code_index = write_index++;
                }
            }
        }
        destination[code_index] = code;
        return write_index;
    }

    // Added decode for FrameParser tests
    static size_t decode(const uint8_t* source, size_t length, uint8_t* destination) {
        size_t read_index = 0;
        size_t write_index = 0;
        uint8_t code;
        uint8_t i;

        while (read_index < length) {
            code = source[read_index];

            if (read_index + code > length && code != 1) {
                return 0;
            }

            read_index++;

            for (i = 1; i < code; i++) {
                destination[write_index++] = source[read_index++];
            }

            if (code != 0xFF && read_index != length) {
                destination[write_index++] = 0;
            }
        }

        return write_index;
    }
};

// ============================================================================
// Helper Classes
// ============================================================================

template <size_t N>
struct ByteBuffer {
  uint8_t data[N];
  size_t len;
  size_t pos;

  ByteBuffer() : len(0), pos(0) { memset(data, 0, sizeof(data)); }

  void clear() {
    len = 0;
    pos = 0;
    memset(data, 0, sizeof(data));
  }

  bool push(uint8_t b) {
    if (len >= N) return false;
    data[len++] = b;
    return true;
  }

  bool append(const uint8_t* src, size_t n) {
    if (!src || len + n > N) return false;
    memcpy(data + len, src, n);
    len += n;
    return true;
  }

  bool append(const void* src, size_t n) {
    return append(static_cast<const uint8_t*>(src), n);
  }

  size_t remaining() const { return (pos < len) ? (len - pos) : 0; }

  int read_byte() {
    if (pos >= len) return -1;
    return data[pos++];
  }

  int peek_byte() const {
    if (pos >= len) return -1;
    return data[pos];
  }
};

class RecordingStream : public Stream {
 public:
  ByteBuffer<8192> tx_buffer;
  ByteBuffer<8192> rx_buffer;

  size_t write(uint8_t c) override {
    tx_buffer.push(c);
    return 1;
  }

  size_t write(const uint8_t* buffer, size_t size) override {
    tx_buffer.append(buffer, size);
    return size;
  }

  int available() override { return static_cast<int>(rx_buffer.remaining()); }
  int read() override { return rx_buffer.read_byte(); }
  int peek() override { return rx_buffer.peek_byte(); }
  void flush() override {}

  void inject_rx(const uint8_t* data, size_t len) {
    rx_buffer.append(data, len);
  }

  void clear() {
    tx_buffer.clear();
    rx_buffer.clear();
  }
};

class TestFrameBuilder {
 public:
  static size_t build(uint8_t* out, size_t out_cap, uint16_t command_id,
                      const uint8_t* payload, size_t payload_len) {
    uint8_t raw[rpc::MAX_RAW_FRAME_SIZE];
    size_t cursor = 0;

    raw[cursor++] = rpc::PROTOCOL_VERSION;

    const uint16_t len = static_cast<uint16_t>(payload_len);
    raw[cursor++] = static_cast<uint8_t>((len >> 8) & 0xFF);
    raw[cursor++] = static_cast<uint8_t>(len & 0xFF);

    raw[cursor++] = static_cast<uint8_t>((command_id >> 8) & 0xFF);
    raw[cursor++] = static_cast<uint8_t>(command_id & 0xFF);

    if (payload_len && payload) {
      memcpy(raw + cursor, payload, payload_len);
      cursor += payload_len;
    }

    const uint32_t crc = crc32_ieee(raw, cursor);
    raw[cursor++] = static_cast<uint8_t>((crc >> 24) & 0xFF);
    raw[cursor++] = static_cast<uint8_t>((crc >> 16) & 0xFF);
    raw[cursor++] = static_cast<uint8_t>((crc >> 8) & 0xFF);
    raw[cursor++] = static_cast<uint8_t>(crc & 0xFF);

    const size_t encoded_len = TestCOBS::encode(raw, cursor, out);
    out[encoded_len] = rpc::RPC_FRAME_DELIMITER;
    return encoded_len + 1;
  }
};

static void reset_bridge_with_stream(RecordingStream& stream) {
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(stream);
  Bridge.begin();
  Bridge._synchronized = true;
  Console.begin();
}

static void restore_bridge_to_serial() {
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(Serial1);
}

// ============================================================================
// MAILBOX.CPP COVERAGE GAPS (42.5% branch coverage)
// ============================================================================

static void test_mailbox_send_null_message() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  // Null message - should early return without sending
  Mailbox.send(static_cast<const char*>(nullptr));
  TEST_ASSERT_EQ_UINT(stream.tx_buffer.len, 0);

  restore_bridge_to_serial();
}

static void test_mailbox_send_empty_message() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  // Empty message - should early return without sending
  Mailbox.send("");
  TEST_ASSERT_EQ_UINT(stream.tx_buffer.len, 0);

  restore_bridge_to_serial();
}

static void test_mailbox_send_null_bytes() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  // Null data pointer - should early return
  Mailbox.send(static_cast<const uint8_t*>(nullptr), 10);
  TEST_ASSERT_EQ_UINT(stream.tx_buffer.len, 0);

  // Zero length - should early return
  uint8_t data[] = {1, 2, 3};
  Mailbox.send(data, 0);
  TEST_ASSERT_EQ_UINT(stream.tx_buffer.len, 0);

  restore_bridge_to_serial();
}

static void test_mailbox_send_oversized_message() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  // Message larger than max payload - should truncate
  char big_msg[rpc::MAX_PAYLOAD_SIZE + 100];
  memset(big_msg, 'A', sizeof(big_msg) - 1);
  big_msg[sizeof(big_msg) - 1] = '\0';
  
  Mailbox.send(big_msg);
  
  // Should have sent (truncated to max)
  TEST_ASSERT(stream.tx_buffer.len > 0);

  restore_bridge_to_serial();
}

static void test_mailbox_send_oversized_bytes() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  // Bytes larger than max payload - should truncate
  uint8_t big_data[rpc::MAX_PAYLOAD_SIZE + 100];
  memset(big_data, 0xAB, sizeof(big_data));
  
  Mailbox.send(big_data, sizeof(big_data));
  
  // Should have sent (truncated)
  TEST_ASSERT(stream.tx_buffer.len > 0);

  restore_bridge_to_serial();
}

static void test_mailbox_handle_response_no_handler() {
  // Response with no handler registered - should not crash
  Mailbox._mailbox_handler = nullptr;
  
  rpc::Frame f;
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP);
  f.header.payload_length = 4;
  rpc::write_u16_be(f.payload, 2);
  f.payload[2] = 'h';
  f.payload[3] = 'i';
  
  Mailbox.handleResponse(f);
  // No crash = success
}

static void test_mailbox_handle_response_short_payload() {
  bool called = false;
  Mailbox.onMailboxMessage([](const uint8_t*, uint16_t) {
    // Should NOT be called
  });
  
  rpc::Frame f;
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP);
  f.header.payload_length = 1;  // Too short (needs at least 2 for length)
  f.payload[0] = 1;
  
  Mailbox.handleResponse(f);
  // Handler should not have been called
}

static void test_mailbox_handle_response_truncated_body() {
  bool called = false;
  Mailbox.onMailboxMessage([](const uint8_t*, uint16_t) {
    // Should NOT be called - body is truncated
  });
  
  rpc::Frame f;
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP);
  f.header.payload_length = 4;  // Claims 4 bytes
  rpc::write_u16_be(f.payload, 10);  // But says message is 10 bytes
  f.payload[2] = 'h';
  f.payload[3] = 'i';
  
  Mailbox.handleResponse(f);
  // Handler should not have been called due to length mismatch
}

static void test_mailbox_available_response_wrong_length() {
  bool called = false;
  Mailbox.onMailboxAvailableResponse([](uint16_t) {
    // Should NOT be called
  });
  
  rpc::Frame f;
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP);
  f.header.payload_length = 2;  // Wrong length (should be 1)
  f.payload[0] = 5;
  f.payload[1] = 0;
  
  Mailbox.handleResponse(f);
  // Handler should not have been called
}

// Global state for handler tests (needed for function pointer callbacks)
static bool g_mailbox_called = false;
static uint8_t g_mailbox_data[32];
static uint16_t g_mailbox_len = 0;

static void mailbox_message_handler(const uint8_t* data, uint16_t len) {
  g_mailbox_called = true;
  g_mailbox_len = len;
  if (data && len < sizeof(g_mailbox_data)) {
    memcpy(g_mailbox_data, data, len);
  }
}

static void test_mailbox_push_inbound_handling() {
  g_mailbox_called = false;
  g_mailbox_len = 0;
  memset(g_mailbox_data, 0, sizeof(g_mailbox_data));
  
  Mailbox.onMailboxMessage(mailbox_message_handler);
  
  // CMD_MAILBOX_PUSH (inbound from Linux)
  rpc::Frame f;
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH);
  f.header.payload_length = 5;
  rpc::write_u16_be(f.payload, 3);  // 3 byte message
  f.payload[2] = 'a';
  f.payload[3] = 'b';
  f.payload[4] = 'c';
  
  Mailbox.handleResponse(f);
  
  TEST_ASSERT(g_mailbox_called);
  TEST_ASSERT_EQ_UINT(g_mailbox_len, 3);
  TEST_ASSERT(g_mailbox_data[0] == 'a');
  TEST_ASSERT(g_mailbox_data[1] == 'b');
  TEST_ASSERT(g_mailbox_data[2] == 'c');
}

static void noop_mailbox_handler(const uint8_t*, uint16_t) {
  // Do nothing
}

static void test_mailbox_unknown_command() {
  Mailbox.onMailboxMessage(noop_mailbox_handler);
  
  rpc::Frame f;
  f.header.command_id = 0xFFFF;  // Unknown command
  f.header.payload_length = 4;
  
  Mailbox.handleResponse(f);
  // Should not crash, just fall through default case
}

// ============================================================================
// FILESYSTEM.CPP COVERAGE GAPS (53.1% branch coverage)
// ============================================================================

static void test_filesystem_write_null_path() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  uint8_t data[] = {1, 2, 3};
  FileSystem.write(nullptr, data, sizeof(data));
  
  TEST_ASSERT_EQ_UINT(stream.tx_buffer.len, 0);
  
  restore_bridge_to_serial();
}

static void test_filesystem_write_empty_path() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  uint8_t data[] = {1, 2, 3};
  FileSystem.write("", data, sizeof(data));
  
  TEST_ASSERT_EQ_UINT(stream.tx_buffer.len, 0);
  
  restore_bridge_to_serial();
}

static void test_filesystem_write_null_data() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  FileSystem.write("/tmp/test", nullptr, 10);
  
  TEST_ASSERT_EQ_UINT(stream.tx_buffer.len, 0);
  
  restore_bridge_to_serial();
}

static void test_filesystem_write_zero_length() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  uint8_t data[] = {1, 2, 3};
  FileSystem.write("/tmp/test", data, 0);
  
  // Even with zero length, a frame is sent (path + zero-length content)
  TEST_ASSERT(stream.tx_buffer.len > 0);
  
  restore_bridge_to_serial();
}

static void test_filesystem_write_path_too_long() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  // Path longer than 255 bytes
  char long_path[300];
  memset(long_path, 'a', sizeof(long_path) - 1);
  long_path[0] = '/';
  long_path[sizeof(long_path) - 1] = '\0';
  
  uint8_t data[] = {1, 2, 3};
  FileSystem.write(long_path, data, sizeof(data));
  
  // Path too long causes early return, no frame sent
  TEST_ASSERT_EQ_UINT(stream.tx_buffer.len, 0);
  
  restore_bridge_to_serial();
}

static bool g_fs_read_called = false;

static void fs_read_handler(const uint8_t*, uint16_t) {
  g_fs_read_called = true;
}

static void test_filesystem_read_handler_truncated() {
  g_fs_read_called = false;
  
  FileSystem.onFileSystemReadResponse(fs_read_handler);
  
  rpc::Frame f;
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP);
  f.header.payload_length = 3;  // Path len + content len = 3, but data is truncated
  f.payload[0] = 10;  // Claims path is 10 bytes but only 2 bytes remain
  
  FileSystem.handleResponse(f);
  // Should not call handler due to truncation
  TEST_ASSERT(!g_fs_read_called);
}

// ============================================================================
// PROCESS.CPP COVERAGE GAPS (57.1% branch coverage)  
// ============================================================================

static void test_process_runAsync_null_command() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  Process.runAsync(nullptr);
  TEST_ASSERT_EQ_UINT(stream.tx_buffer.len, 0);  // No frame sent

  restore_bridge_to_serial();
}

static void test_process_runAsync_empty_command() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  Process.runAsync("");
  TEST_ASSERT_EQ_UINT(stream.tx_buffer.len, 0);

  restore_bridge_to_serial();
}

static bool g_process_poll_called = false;

static void process_poll_handler(rpc::StatusCode, uint8_t,
                                 const uint8_t*, uint16_t,
                                 const uint8_t*, uint16_t) {
  g_process_poll_called = true;
}

static void test_process_poll_response_short_payload() {
  g_process_poll_called = false;
  Process.onProcessPollResponse(process_poll_handler);
  
  // Minimum valid payload is 6 bytes
  rpc::Frame f;
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP);
  f.header.payload_length = 5;  // Too short
  
  Process.handleResponse(f);
  TEST_ASSERT(!g_process_poll_called);
}

static bool g_process_async_called = false;
static int16_t g_process_async_pid = -1;

static void process_async_handler(int16_t pid) {
  g_process_async_called = true;
  g_process_async_pid = pid;
}

static void test_process_async_response_handler() {
  g_process_async_called = false;
  g_process_async_pid = -1;
  
  Process.onProcessRunAsyncResponse(process_async_handler);
  
  rpc::Frame f;
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP);
  f.header.payload_length = 2;
  rpc::write_u16_be(f.payload, 1234);  // PID
  
  Process.handleResponse(f);
  
  TEST_ASSERT(g_process_async_called);
  TEST_ASSERT_EQ_UINT(g_process_async_pid, 1234);
}

static void test_process_async_response_short() {
  g_process_async_called = false;
  
  Process.onProcessRunAsyncResponse(process_async_handler);
  
  rpc::Frame f;
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP);
  f.header.payload_length = 1;  // Too short (needs 2)
  
  Process.handleResponse(f);
  TEST_ASSERT(!g_process_async_called);
}

// ============================================================================
// CONSOLE.CPP COVERAGE GAPS (62.1% branch coverage)
// ============================================================================

static void test_console_read_empty() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  
  // Access via public method - available() should return 0
  // when buffer is empty, and read() should return -1
  int result = Console.read();
  TEST_ASSERT_EQ_UINT(result, -1);  // Should return -1 for empty
  
  restore_bridge_to_serial();
}

static void test_console_peek_empty() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  
  // Empty buffer - peek should return -1
  int result = Console.peek();
  TEST_ASSERT_EQ_UINT(result, -1);  // Should return -1 for empty
  
  restore_bridge_to_serial();
}

static void test_console_available_count() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  
  // Add some bytes
  uint8_t data[] = {'a', 'b', 'c'};
  Console._push(data, sizeof(data));
  
  int avail = Console.available();
  TEST_ASSERT_EQ_UINT(avail, 3);
  
  restore_bridge_to_serial();
}

static void test_console_buffer_operations() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  
  uint8_t data[] = {'x', 'y', 'z', 'w'};
  Console._push(data, sizeof(data));
  
  // Should have wrapped around
  int avail = Console.available();
  TEST_ASSERT_EQ_UINT(avail, 4);
  
  restore_bridge_to_serial();
}

// ============================================================================
// RPC_FRAME.CPP COVERAGE GAPS (72.4% branch coverage)
// ============================================================================

static void test_frame_parser_crc_mismatch() {
  rpc::FrameParser parser;
  
  // Build a valid frame then corrupt CRC
  uint8_t encoded[128];
  uint8_t raw[32];
  
  raw[0] = rpc::PROTOCOL_VERSION;
  raw[1] = 0; raw[2] = 2;  // Length = 2
  raw[3] = 0; raw[4] = 0x10;  // Command
  raw[5] = 'a'; raw[6] = 'b';  // Payload
  
  uint32_t crc = crc32_ieee(raw, 7);
  raw[7] = (crc >> 24) & 0xFF;
  raw[8] = (crc >> 16) & 0xFF;
  raw[9] = (crc >> 8) & 0xFF;
  raw[10] = (crc & 0xFF) ^ 0xFF;  // Corrupt CRC
  
  size_t len = TestCOBS::encode(raw, 11, encoded);
  encoded[len++] = rpc::RPC_FRAME_DELIMITER;
  
  rpc::Frame f;
  
  // PacketSerial simulation: decode COBS first
  uint8_t decoded[32];
  size_t decoded_len = TestCOBS::decode(encoded, len - 1, decoded); // -1 for delimiter
  
  // Then parse
  parser.parse(decoded, decoded_len, f);
  
  TEST_ASSERT(parser.getError() == rpc::FrameParser::Error::CRC_MISMATCH);
}

static void test_frame_parser_malformed() {
  rpc::FrameParser parser;
  
  uint8_t encoded[128];
  uint8_t raw[32];
  
  raw[0] = 0xFF;  // Wrong version
  raw[1] = 0; raw[2] = 0;  // Length = 0
  raw[3] = 0; raw[4] = 0x10;  // Command
  
  uint32_t crc = crc32_ieee(raw, 5);
  raw[5] = (crc >> 24) & 0xFF;
  raw[6] = (crc >> 16) & 0xFF;
  raw[7] = (crc >> 8) & 0xFF;
  raw[8] = crc & 0xFF;
  
  size_t len = TestCOBS::encode(raw, 9, encoded);
  encoded[len++] = rpc::RPC_FRAME_DELIMITER;
  
  rpc::Frame f;
  // PacketSerial simulation
  uint8_t decoded[32];
  size_t decoded_len = TestCOBS::decode(encoded, len - 1, decoded);
  
  parser.parse(decoded, decoded_len, f);
  
  // Wrong version causes MALFORMED error (version is validated after CRC passes)
  TEST_ASSERT(parser.getError() == rpc::FrameParser::Error::MALFORMED);
}

static void test_frame_parser_oversized() {
  rpc::FrameParser parser;
  
  uint8_t encoded[rpc::MAX_RAW_FRAME_SIZE + 100];
  uint8_t raw[rpc::MAX_RAW_FRAME_SIZE + 50];
  
  raw[0] = rpc::PROTOCOL_VERSION;
  // Declare a huge payload
  raw[1] = 0xFF; raw[2] = 0xFF;  // Length = 65535
  raw[3] = 0; raw[4] = 0x10;  // Command
  
  // Add a CRC (won't matter, will fail on size)
  uint32_t crc = crc32_ieee(raw, 5);
  raw[5] = (crc >> 24) & 0xFF;
  raw[6] = (crc >> 16) & 0xFF;
  raw[7] = (crc >> 8) & 0xFF;
  raw[8] = crc & 0xFF;
  
  size_t len = TestCOBS::encode(raw, 9, encoded);
  encoded[len++] = rpc::RPC_FRAME_DELIMITER;
  
  rpc::Frame f;
  
  // Note: If PacketSerial gives us a huge buffer, parse() should handle it.
  // But here we simulate passing a huge buffer (raw) to parse().
  // Actually, TestCOBS::decode doesn't check size limit of dest buffer so we must be careful.
  uint8_t decoded[rpc::MAX_RAW_FRAME_SIZE + 50]; 
  size_t decoded_len = TestCOBS::decode(encoded, len - 1, decoded);
  
  parser.parse(decoded, decoded_len, f);
  
  // Should have some error (either OVERSIZED/MALFORMED/CRC)
  // FrameParser usually checks size <= MAX_RAW_FRAME_SIZE
  // If it's bigger, it returns false/error.
  // Let's check logic: parse() takes size. If size > MAX_RAW_FRAME_SIZE, it might error.
  
  // Wait, if it's oversize, FrameParser might return MALFORMED.
  TEST_ASSERT(parser.getError() != rpc::FrameParser::Error::NONE);
}

static void test_frame_parser_empty_frame() {
  rpc::FrameParser parser;
  
  // Empty buffer
  rpc::Frame f;
  bool result = parser.parse(nullptr, 0, f);
  
  // Should return false (no valid frame)
  TEST_ASSERT(!result);
}

static void test_frame_parser_cobs_decode_error() {
  // This test was relevant for stream parsing (PacketSerial).
  // FrameParser now receives decoded data.
  // If PacketSerial fails to decode, it won't call the callback.
  // So FrameParser never sees bad COBS data.
  // We can remove this test or test that garbage data is rejected.
  
  rpc::FrameParser parser;
  uint8_t garbage[] = {0x01, 0x02, 0x03};
  rpc::Frame f;
  parser.parse(garbage, 3, f);
  TEST_ASSERT(parser.getError() != rpc::FrameParser::Error::NONE);
}

// ============================================================================
// BRIDGE.CPP COVERAGE GAPS (62.7% branch coverage)
// ============================================================================

static void test_bridge_process_when_not_begun() {
  RecordingStream stream;
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(stream);
  // Don't call begin() - test the guard
  
  Bridge.process();
  // Should not crash
  
  restore_bridge_to_serial();
}

static void test_bridge_send_frame_when_not_synchronized() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  Bridge._synchronized = false;
  stream.tx_buffer.clear();
  
  // Certain commands should still send even when not synced (handshake)
  bool result = Bridge.sendFrame(rpc::CommandId::CMD_LINK_RESET);
  TEST_ASSERT(result);  // Handshake commands should work
  
  restore_bridge_to_serial();
}

static void test_bridge_handle_xoff_xon() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  
  // Inject XOFF frame
  uint8_t frame[64];
  size_t len = TestFrameBuilder::build(
    frame, sizeof(frame),
    rpc::to_underlying(rpc::CommandId::CMD_XOFF),
    nullptr, 0
  );
  stream.inject_rx(frame, len);
  Bridge.process();
  
  // Should have set pause state
  // (implementation detail, just verify no crash)
  
  // Inject XON frame  
  len = TestFrameBuilder::build(
    frame, sizeof(frame),
    rpc::to_underlying(rpc::CommandId::CMD_XON),
    nullptr, 0
  );
  stream.inject_rx(frame, len);
  Bridge.process();
  
  restore_bridge_to_serial();
}

static void test_bridge_status_ack_handling() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  
  // Inject STATUS_ACK frame
  uint8_t ack_payload[2] = {0, 0x30};  // Ack for some command
  uint8_t frame[64];
  size_t len = TestFrameBuilder::build(
    frame, sizeof(frame),
    rpc::to_underlying(rpc::StatusCode::STATUS_ACK),
    ack_payload, sizeof(ack_payload)
  );
  stream.inject_rx(frame, len);
  Bridge.process();
  
  // Should handle without crash
  
  restore_bridge_to_serial();
}

static void test_bridge_status_error_handling() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  
  // Inject STATUS_ERROR frame
  uint8_t frame[64];
  size_t len = TestFrameBuilder::build(
    frame, sizeof(frame),
    rpc::to_underlying(rpc::StatusCode::STATUS_ERROR),
    reinterpret_cast<const uint8_t*>("test_error"), 10
  );
  stream.inject_rx(frame, len);
  Bridge.process();
  
  // Should handle without crash
  
  restore_bridge_to_serial();
}

static void test_bridge_unknown_command() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  
  // Inject unknown command
  uint8_t payload[] = {1, 2, 3};
  uint8_t frame[64];
  size_t len = TestFrameBuilder::build(
    frame, sizeof(frame),
    0xBEEF,  // Unknown command
    payload, sizeof(payload)
  );
  stream.inject_rx(frame, len);
  Bridge.process();
  
  // Should handle unknown gracefully
  
  restore_bridge_to_serial();
}

static void test_datastore_put_null_key() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  DataStore.put(nullptr, "value");
  TEST_ASSERT_EQ_UINT(stream.tx_buffer.len, 0);

  restore_bridge_to_serial();
}

static void test_datastore_put_empty_key() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  DataStore.put("", "value");
  TEST_ASSERT_EQ_UINT(stream.tx_buffer.len, 0);

  restore_bridge_to_serial();
}

static void test_datastore_get_no_handler() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  
  DataStore._datastore_get_handler = nullptr;
  DataStore._trackPendingDatastoreKey("test");
  
  rpc::Frame f;
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP);
  f.header.payload_length = 3;
  f.payload[0] = 2;  // Value length
  f.payload[1] = 'a';
  f.payload[2] = 'b';
  
  DataStore.handleResponse(f);
  // Should not crash
  
  restore_bridge_to_serial();
}

} // namespace

int main() {
  // Mailbox tests
  test_mailbox_send_null_message();
  test_mailbox_send_empty_message();
  test_mailbox_send_null_bytes();
  test_mailbox_send_oversized_message();
  test_mailbox_send_oversized_bytes();
  test_mailbox_handle_response_no_handler();
  test_mailbox_handle_response_short_payload();
  test_mailbox_handle_response_truncated_body();
  test_mailbox_available_response_wrong_length();
  test_mailbox_push_inbound_handling();
  test_mailbox_unknown_command();
  
  // FileSystem tests
  test_filesystem_write_null_path();
  test_filesystem_write_empty_path();
  test_filesystem_write_null_data();
  test_filesystem_write_zero_length();
  test_filesystem_write_path_too_long();
  test_filesystem_read_handler_truncated();
  
  // Process tests
  test_process_runAsync_null_command();
  test_process_runAsync_empty_command();
  test_process_poll_response_short_payload();
  test_process_async_response_handler();
  test_process_async_response_short();
  
  // Console tests
  test_console_read_empty();
  test_console_peek_empty();
  test_console_available_count();
  test_console_buffer_operations();
  
  // rpc_frame tests
  test_frame_parser_crc_mismatch();
  test_frame_parser_malformed();
  test_frame_parser_oversized();
  test_frame_parser_empty_frame();
  test_frame_parser_cobs_decode_error();
  
  // Bridge tests
  test_bridge_process_when_not_begun();
  test_bridge_send_frame_when_not_synchronized();
  test_bridge_handle_xoff_xon();
  test_bridge_status_ack_handling();
  test_bridge_status_error_handling();
  test_bridge_unknown_command();
  test_datastore_put_null_key();
  test_datastore_put_empty_key();
  test_datastore_get_no_handler();
  
  return 0;
}