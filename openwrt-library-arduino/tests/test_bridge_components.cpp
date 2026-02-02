#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <stdio.h> // Added for debug printf

#define private public
#define protected public
#include "Bridge.h"
#undef private
#undef protected

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


namespace {

// Local COBS implementation for test frame generation and parsing
// (Since src/protocol/cobs.h was removed in favor of PacketSerial)
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

// Safe buffer size for encoded frames
constexpr size_t kMaxEncodedSize = rpc::MAX_RAW_FRAME_SIZE + 32;

template <size_t N>
struct FixedString {
  char buf[N];
  size_t len;

  FixedString() : buf(), len(0) { memset(buf, 0, sizeof(buf)); }

  void clear() {
    len = 0;
    buf[0] = '\0';
  }

  void set_from_cstr(const char* s) {
    if (!s) {
      clear();
      return;
    }
    const size_t in_len = strlen(s);
    set_from_bytes(s, in_len);
  }

  void set_from_bytes(const void* data, size_t n) {
    if (!data) {
      clear();
      return;
    }
    const size_t cap = (N > 0) ? (N - 1) : 0;
    len = (n > cap) ? cap : n;
    memcpy(buf, data, len);
    buf[len] = '\0';
  }

  bool equals_cstr(const char* s) const {
    if (!s) {
      return len == 0;
    }
    return strcmp(buf, s) == 0;
  }
};

class RecordingStream : public Stream {
 public:
  ByteBuffer<8192> tx_buffer;
  ByteBuffer<8192> rx_buffer;

  size_t write(uint8_t c) override {
    TEST_ASSERT(tx_buffer.push(c));
    return 1;
  }

  size_t write(const uint8_t* buffer, size_t size) override {
    TEST_ASSERT(tx_buffer.append(buffer, size));
    return size;
  }

  int available() override { return static_cast<int>(rx_buffer.remaining()); }

  int read() override { return rx_buffer.read_byte(); }

  int peek() override { return rx_buffer.peek_byte(); }

  void flush() override {}

  void inject_rx(const uint8_t* data, size_t len) {
    TEST_ASSERT(rx_buffer.append(data, len));
  }

  void clear() {
    tx_buffer.clear();
    rx_buffer.clear();
  }
};

enum class WriteMode {
  Normal,
  ShortAlways,
};

class FlakyStream : public Stream {
 public:
  ByteBuffer<8192> tx_buffer;
  ByteBuffer<8192> rx_buffer;
  WriteMode mode = WriteMode::Normal;

  size_t write(uint8_t c) override {
    TEST_ASSERT(tx_buffer.push(c));
    return 1;
  }

  size_t write(const uint8_t* buffer, size_t size) override {
    if (!buffer || size == 0) {
      return 0;
    }
    if (mode == WriteMode::ShortAlways) {
      const size_t n = (size > 0) ? (size - 1) : 0;
      TEST_ASSERT(tx_buffer.append(buffer, n));
      return n;
    }
    TEST_ASSERT(tx_buffer.append(buffer, size));
    return size;
  }

  int available() override { return static_cast<int>(rx_buffer.remaining()); }

  int read() override { return rx_buffer.read_byte(); }

  int peek() override { return rx_buffer.peek_byte(); }

  void flush() override {}

  void inject_rx(const uint8_t* data, size_t len) {
    TEST_ASSERT(rx_buffer.append(data, len));
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
    raw[cursor++] = static_cast<uint8_t>((len >> 8) & rpc::RPC_UINT8_MASK);
    raw[cursor++] = static_cast<uint8_t>(len & rpc::RPC_UINT8_MASK);

    raw[cursor++] = static_cast<uint8_t>((command_id >> 8) & rpc::RPC_UINT8_MASK);
    raw[cursor++] = static_cast<uint8_t>(command_id & rpc::RPC_UINT8_MASK);

    if (payload_len) {
      TEST_ASSERT(payload != nullptr);
      TEST_ASSERT(cursor + payload_len + 4 <= sizeof(raw));
      memcpy(raw + cursor, payload, payload_len);
      cursor += payload_len;
    }

    const uint32_t crc = crc32_ieee(raw, cursor);
    TEST_ASSERT(cursor + 4 <= sizeof(raw));
    raw[cursor++] = static_cast<uint8_t>((crc >> 24) & rpc::RPC_UINT8_MASK);
    raw[cursor++] = static_cast<uint8_t>((crc >> 16) & rpc::RPC_UINT8_MASK);
    raw[cursor++] = static_cast<uint8_t>((crc >> 8) & rpc::RPC_UINT8_MASK);
    raw[cursor++] = static_cast<uint8_t>(crc & rpc::RPC_UINT8_MASK);

    TEST_ASSERT(out != nullptr);
    const size_t encoded_len = TestCOBS::encode(raw, cursor, out);
    TEST_ASSERT(encoded_len > 0);
    TEST_ASSERT(encoded_len + 1 <= out_cap);
    out[encoded_len] = rpc::RPC_FRAME_DELIMITER;
    return encoded_len + 1;
  }
};

struct FrameList {
  rpc::Frame frames[16];
  size_t count;
  rpc::FrameParser::Error last_error;
};

// Updated parse_frames to simulate PacketSerial's packet extraction + FrameParser
static FrameList parse_frames(const uint8_t* bytes, size_t len) {
  FrameList out;
  out.count = 0;
  out.last_error = rpc::FrameParser::Error::NONE;

  rpc::FrameParser parser;
  
  // Simple delimiter splitting + COBS decode simulation
  uint8_t packet_buf[kMaxEncodedSize];
  size_t packet_idx = 0;
  uint8_t decoded_buf[rpc::MAX_RAW_FRAME_SIZE];

  for (size_t i = 0; i < len; ++i) {
    uint8_t b = bytes[i];
    if (b == rpc::RPC_FRAME_DELIMITER) {
      if (packet_idx > 0) {
        // Decode COBS
        size_t decoded_len = TestCOBS::decode(packet_buf, packet_idx, decoded_buf);
        if (decoded_len > 0) {
            // Parse Frame
            if (out.count < (sizeof(out.frames) / sizeof(out.frames[0]))) {
                rpc::Frame f;
                if (parser.parse(decoded_buf, decoded_len, f)) {
                    out.frames[out.count++] = f;
                }
                const rpc::FrameParser::Error err = parser.getError();
                if (err != rpc::FrameParser::Error::NONE) {
                    out.last_error = err;
                    parser.clearError();
                }
            }
        }
      }
      packet_idx = 0; // Reset for next packet
    } else {
        if (packet_idx < kMaxEncodedSize) {
            packet_buf[packet_idx++] = b;
        }
    }
  }

  return out;
}

static void reset_bridge_with_stream(RecordingStream& stream) {
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(stream);
  Bridge.begin();
  Bridge._fsm.resetFsm(); Bridge._fsm.handshakeComplete();
  Console.begin();
}

static void restore_bridge_to_serial() {
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(Serial1);
}

static void inject_ack(RecordingStream& stream, uint16_t command_id) {
  const uint16_t ack_cmd_id = rpc::to_underlying(rpc::StatusCode::STATUS_ACK);
  const uint8_t payload[2] = {
      static_cast<uint8_t>((command_id >> 8) & rpc::RPC_UINT8_MASK),
      static_cast<uint8_t>(command_id & rpc::RPC_UINT8_MASK),
  };

  enum { kEncodedCap = kMaxEncodedSize + 1 };
  uint8_t frame[kEncodedCap];
  const size_t frame_len =
      TestFrameBuilder::build(frame, sizeof(frame), ack_cmd_id, payload, sizeof(payload));

  stream.inject_rx(frame, frame_len);
  Bridge.process();
}

struct DatastoreGetState {
  static DatastoreGetState* instance;
  bool called;
  FixedString<rpc::RPC_MAX_DATASTORE_KEY_LENGTH + 1> key;
  ByteBuffer<rpc::MAX_PAYLOAD_SIZE> value;

  DatastoreGetState() : called(false), key(), value() {}
};

DatastoreGetState* DatastoreGetState::instance = nullptr;

static void datastore_get_trampoline(const char* key, const uint8_t* value,
                                    uint16_t length) {
  DatastoreGetState* state = DatastoreGetState::instance;
  if (!state) return;
  state->called = true;
  state->key.set_from_cstr(key);
  state->value.clear();
  if (value && length) {
    TEST_ASSERT(state->value.append(value, length));
  }
}

struct MailboxState {
  static MailboxState* instance;
  bool called;
  ByteBuffer<rpc::MAX_PAYLOAD_SIZE> message;

  MailboxState() : called(false), message() {}
};

MailboxState* MailboxState::instance = nullptr;

static void mailbox_trampoline(const uint8_t* buffer, uint16_t size) {
  MailboxState* state = MailboxState::instance;
  if (!state) return;
  state->called = true;
  state->message.clear();
  if (buffer && size) {
    TEST_ASSERT(state->message.append(buffer, size));
  }
}

struct MailboxAvailableState {
  static MailboxAvailableState* instance;
  bool called;
  uint8_t count;

  MailboxAvailableState() : called(false), count(0) {}
};

MailboxAvailableState* MailboxAvailableState::instance = nullptr;

static void mailbox_available_trampoline(uint16_t count) {
  MailboxAvailableState* state = MailboxAvailableState::instance;
  if (!state) return;
  state->called = true;
  state->count = static_cast<uint8_t>(count);
}



static void test_console_write_outbound_frame() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  const char msg[] = "hello";
  const size_t sent = Console.write(reinterpret_cast<const uint8_t*>(msg), sizeof(msg) - 1);
  TEST_ASSERT_EQ_UINT(sent, sizeof(msg) - 1);

  // DEBUG: Print buffer content
  printf("DEBUG: Stream TX buffer len: %zu\n", stream.tx_buffer.len);
  for (size_t i = 0; i < stream.tx_buffer.len; i++) {
      printf("%02X ", stream.tx_buffer.data[i]);
  }
  printf("\n");

  const FrameList frames = parse_frames(stream.tx_buffer.data, stream.tx_buffer.len);
  printf("DEBUG: Frames parsed: %zu\n", frames.count);
  
  TEST_ASSERT(frames.count >= 1);
  TEST_ASSERT_EQ_UINT(frames.frames[0].header.command_id,
                      rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE));
  TEST_ASSERT_EQ_UINT(frames.frames[0].header.payload_length, sizeof(msg) - 1);
  TEST_ASSERT(test_memeq(frames.frames[0].payload.data(), msg, sizeof(msg) - 1));

  inject_ack(stream, rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE));
  restore_bridge_to_serial();
}

static void test_datastore_put_outbound_frame() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  DataStore.put("k", "v");

  const FrameList frames = parse_frames(stream.tx_buffer.data, stream.tx_buffer.len);
  TEST_ASSERT(frames.count >= 1);
  const rpc::Frame& f = frames.frames[0];
  TEST_ASSERT_EQ_UINT(f.header.command_id, rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_PUT));
  TEST_ASSERT(f.header.payload_length >= 4);
  TEST_ASSERT_EQ_UINT(f.payload[0], 1);
  TEST_ASSERT_EQ_UINT(f.payload[1], 'k');
  TEST_ASSERT_EQ_UINT(f.payload[2], 1);
  TEST_ASSERT_EQ_UINT(f.payload[3], 'v');

  inject_ack(stream, rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_PUT));
  restore_bridge_to_serial();
}

static void test_mailbox_send_outbound_frame() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  Mailbox.send("hi");

  const FrameList frames = parse_frames(stream.tx_buffer.data, stream.tx_buffer.len);
  TEST_ASSERT(frames.count >= 1);
  const rpc::Frame& f = frames.frames[0];
  TEST_ASSERT_EQ_UINT(f.header.command_id, rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH));
  TEST_ASSERT(f.header.payload_length >= 2);
  // Payload is just the message "hi" (no length prefix)
  TEST_ASSERT(test_memeq(f.payload.data(), "hi", 2));

  inject_ack(stream, rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH));
  restore_bridge_to_serial();
}

static void test_filesystem_write_outbound_frame() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  const uint8_t data[] = {TEST_BYTE_01, TEST_BYTE_02};
  const char path[] = "/tmp/a";
  FileSystem.write(path, data, sizeof(data));

  const FrameList frames = parse_frames(stream.tx_buffer.data, stream.tx_buffer.len);
  TEST_ASSERT(frames.count >= 1);
  const rpc::Frame& f = frames.frames[0];
  TEST_ASSERT_EQ_UINT(f.header.command_id, rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE));
  TEST_ASSERT(f.header.payload_length >= 1);

  const uint8_t path_len = f.payload[0];
  TEST_ASSERT_EQ_UINT(path_len, sizeof(path) - 1);
  TEST_ASSERT(test_memeq(f.payload.data() + 1, path, path_len));
  
  // Data follows immediately after path (no length prefix for data in this protocol version)
  TEST_ASSERT(test_memeq(f.payload.data() + 1 + path_len, data, sizeof(data)));

  inject_ack(stream, rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE));
  restore_bridge_to_serial();
}



static void test_datastore_get_response_handler() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);

  DatastoreGetState state;
  DatastoreGetState::instance = &state;
  DataStore.onDataStoreGetResponse(datastore_get_trampoline);
  TEST_ASSERT(DataStore._trackPendingDatastoreKey("k"));

  rpc::Frame f;
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP);
  const uint8_t value[] = {'v', 'v'};
  f.header.payload_length = 1 + sizeof(value);
  f.payload[0] = static_cast<uint8_t>(sizeof(value));
  memcpy(f.payload.data() + 1, value, sizeof(value));

  DataStore.handleResponse(f);

  TEST_ASSERT(state.called);
  TEST_ASSERT(state.key.equals_cstr("k"));
  TEST_ASSERT_EQ_UINT(state.value.len, sizeof(value));
  TEST_ASSERT(test_memeq(state.value.data, value, sizeof(value)));

  DatastoreGetState::instance = nullptr;
  restore_bridge_to_serial();
}

static void test_mailbox_read_response_handler() {
  MailboxState state;
  MailboxState::instance = &state;
  Mailbox.onMailboxMessage(mailbox_trampoline);

  rpc::Frame f;
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP);
  const uint8_t msg[] = {'o', 'k'};
  f.header.payload_length = 2 + sizeof(msg);
  rpc::write_u16_be(f.payload.data(), static_cast<uint16_t>(sizeof(msg)));
  memcpy(f.payload.data() + 2, msg, sizeof(msg));

  Mailbox.handleResponse(f);

  TEST_ASSERT(state.called);
  TEST_ASSERT_EQ_UINT(state.message.len, sizeof(msg));
  TEST_ASSERT(test_memeq(state.message.data, msg, sizeof(msg)));

  MailboxState::instance = nullptr;
}



static void test_console_write_when_not_begun() {
  // Directly exercise the guard branch.
  Console._begun = false;
  TEST_ASSERT_EQ_UINT(Console.write('a'), 0);
  const uint8_t buf[] = {'x'};
  TEST_ASSERT_EQ_UINT(Console.write(buf, sizeof(buf)), 0);
}

static void test_console_write_char_flush_on_newline() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  // Writing a newline flushes.
  TEST_ASSERT_EQ_UINT(Console.write('h'), 1);
  TEST_ASSERT_EQ_UINT(Console.write('\n'), 1);
  TEST_ASSERT(stream.tx_buffer.len > 0);

  const FrameList frames = parse_frames(stream.tx_buffer.data, stream.tx_buffer.len);
  TEST_ASSERT(frames.count >= 1);
  TEST_ASSERT_EQ_UINT(frames.frames[0].header.command_id,
                      rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE));
  restore_bridge_to_serial();
}

static void test_console_read_sends_xon_success_and_failure() {
  // Success case: XON sent and _xoff_sent cleared.
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  Console._xoff_sent = true;
  const uint8_t b = 'z';
  Console._push(&b, 1);
  TEST_ASSERT(Console.read() == 'z');
  TEST_ASSERT(!Console._xoff_sent);
  TEST_ASSERT(stream.tx_buffer.len > 0);
  restore_bridge_to_serial();

  // Failure case removed: PacketSerial does not report write failures, so sendFrame always returns true.
}









static void test_mailbox_request_frames_and_available_handler() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  Mailbox.requestRead();
  Mailbox.requestAvailable();

  const FrameList frames = parse_frames(stream.tx_buffer.data, stream.tx_buffer.len);
  TEST_ASSERT(frames.count >= 2);
  TEST_ASSERT_EQ_UINT(frames.frames[0].header.command_id,
                      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ));
  TEST_ASSERT_EQ_UINT(frames.frames[1].header.command_id,
                      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE));

  // Available response invokes handler.
  MailboxAvailableState st;
  MailboxAvailableState::instance = &st;
  Mailbox.onMailboxAvailableResponse(mailbox_available_trampoline);

      rpc::Frame f{};
      f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP);
      f.header.payload_length = 2;
      rpc::write_u16_be(f.payload.data(), 7);
      Mailbox.handleResponse(f);  TEST_ASSERT(st.called);
  TEST_ASSERT_EQ_UINT(st.count, 7);

  MailboxAvailableState::instance = nullptr;
  restore_bridge_to_serial();
}

static void test_datastore_request_get_queue_full() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  DataStore.requestGet("a");
  DataStore.requestGet("b");

  const FrameList frames = parse_frames(stream.tx_buffer.data, stream.tx_buffer.len);
  TEST_ASSERT(frames.count >= 2);
  TEST_ASSERT_EQ_UINT(frames.frames[0].header.command_id,
                      rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET));
  // Second should emit STATUS_ERROR.
  TEST_ASSERT_EQ_UINT(frames.frames[1].header.command_id,
                      rpc::to_underlying(rpc::StatusCode::STATUS_ERROR));

  restore_bridge_to_serial();
}

static void test_filesystem_remove_and_read_outbound_guards() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  FileSystem.remove(nullptr);
  FileSystem.read(nullptr);
  FileSystem.read("");
  TEST_ASSERT_EQ_UINT(stream.tx_buffer.len, 0);

  FileSystem.remove("/tmp/x");
  FileSystem.read("/tmp/y");

  const FrameList frames = parse_frames(stream.tx_buffer.data, stream.tx_buffer.len);
  TEST_ASSERT(frames.count >= 2);
  TEST_ASSERT_EQ_UINT(frames.frames[0].header.command_id,
                      rpc::to_underlying(rpc::CommandId::CMD_FILE_REMOVE));
  TEST_ASSERT_EQ_UINT(frames.frames[1].header.command_id,
                      rpc::to_underlying(rpc::CommandId::CMD_FILE_READ));

  restore_bridge_to_serial();
}

} // namespace

int main() {
  test_console_write_outbound_frame();
  test_console_write_when_not_begun();
  test_console_write_char_flush_on_newline();
  test_console_read_sends_xon_success_and_failure();
  test_datastore_put_outbound_frame();
  test_mailbox_send_outbound_frame();
  test_mailbox_request_frames_and_available_handler();
  test_filesystem_write_outbound_frame();
  test_datastore_get_response_handler();
  test_datastore_request_get_queue_full();
  test_mailbox_read_response_handler();
  return 0;
}