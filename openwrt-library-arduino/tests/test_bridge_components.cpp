#include <iostream>
#include <cstdlib>
#define TEST_ASSERT(cond) if(!(cond)) { std::cerr << "[FATAL] Assertion failed at line " << __LINE__ << ": " << #cond << std::endl; std::abort(); }

#include <climits>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <cassert>
#include <new>
#include <string>
#include <vector>
#include <iostream>

// CRITICAL: Define this BEFORE includes to access private members
#define BRIDGE_HOST_TEST 1

#include "Bridge.h"
#include "protocol/rpc_protocol.h"
#include "protocol/cobs.h"
#include "protocol/crc.h"
#include "protocol/rpc_frame.h"
#include "test_constants.h"

// Debug trace macro
#define TEST_TRACE(msg) std::cout << "[TEST] " << msg << std::endl;

// Define global Serial instances for the stub
HardwareSerial Serial;
HardwareSerial Serial1;

// Define Bridge globals here to ensure correct initialization order
BridgeClass Bridge(Serial1);
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;

using namespace rpc;

constexpr uint16_t command_value(CommandId command) {
  return to_underlying(command);
}

constexpr uint8_t status_value(StatusCode status) {
  return to_underlying(status);
}

namespace {

struct DatastoreHandlerState {
  static DatastoreHandlerState* instance;
  bool called = false;
  std::string key;
  std::string value;
};

DatastoreHandlerState* DatastoreHandlerState::instance = nullptr;

void datastore_handler_trampoline(
    const char* key, const uint8_t* value, uint16_t length) {
  auto* state = DatastoreHandlerState::instance;
  if (!state) return;
  state->called = true;
  state->key = key ? key : "";
  if (value && length > 0) {
      state->value.assign(
          reinterpret_cast<const char*>(value),
          reinterpret_cast<const char*>(value) + length);
  } else {
      state->value.clear();
  }
}

struct MailboxHandlerState {
  static MailboxHandlerState* instance;
  bool called = false;
  std::string message;
};

MailboxHandlerState* MailboxHandlerState::instance = nullptr;

void mailbox_handler_trampoline(const uint8_t* buffer, uint16_t size) {
  auto* state = MailboxHandlerState::instance;
  if (!state) return;
  state->called = true;
  if (buffer && size > 0) {
      state->message.assign(reinterpret_cast<const char*>(buffer), size);
  } else {
      state->message.clear();
  }
}

struct ProcessPollHandlerState {
  static ProcessPollHandlerState* instance;
  bool called = false;
  StatusCode status = StatusCode::STATUS_ERROR;
  uint8_t exit_code = rpc::RPC_PROCESS_DEFAULT_EXIT_CODE;
  std::string stdout_text;
  std::string stderr_text;
  int pid_to_requeue = -1;
};

ProcessPollHandlerState* ProcessPollHandlerState::instance = nullptr;

void process_poll_handler_trampoline(
  StatusCode status,
    uint8_t exit_code,
    const uint8_t* stdout_data,
    uint16_t stdout_len,
    const uint8_t* stderr_data,
    uint16_t stderr_len) {
  auto* state = ProcessPollHandlerState::instance;
  if (!state) return;
  state->called = true;
  state->status = status;
  state->exit_code = exit_code;
  
  if (stdout_data && stdout_len > 0) {
      state->stdout_text.assign(
          reinterpret_cast<const char*>(stdout_data), stdout_len);
  } else {
      state->stdout_text.clear();
  }

  if (stderr_data && stderr_len > 0) {
      state->stderr_text.assign(
          reinterpret_cast<const char*>(stderr_data), stderr_len);
  } else {
      state->stderr_text.clear();
  }

  if (state->pid_to_requeue >= 0 && exit_code == TEST_EXIT_CODE) {
      Process.poll(state->pid_to_requeue);
  }
}

struct StatusHandlerState {
  static StatusHandlerState* instance;
  bool called = false;
  StatusCode status_code = StatusCode::STATUS_OK;
  std::string payload;
};

StatusHandlerState* StatusHandlerState::instance = nullptr;

void status_handler_trampoline(
    StatusCode status_code, const uint8_t* payload, uint16_t length) {
  auto* state = StatusHandlerState::instance;
  if (!state) return;
  state->called = true;
  state->status_code = status_code;
  if (payload && length > 0) {
      state->payload.assign(
          reinterpret_cast<const char*>(payload),
          reinterpret_cast<const char*>(payload) + length);
  } else {
      state->payload.clear();
  }
}

class RecordingStream : public Stream {
public:
    std::vector<uint8_t> tx_buffer;
    std::vector<uint8_t> rx_buffer;
    size_t rx_pos = 0;

    size_t write(uint8_t c) override {
        tx_buffer.push_back(c);
        return 1;
    }

    size_t write(const uint8_t* buffer, size_t size) override {
        tx_buffer.insert(tx_buffer.end(), buffer, buffer + size);
        return size;
    }

    int available() override {
        return static_cast<int>(rx_buffer.size() - rx_pos);
    }

    int read() override {
        if (rx_pos >= rx_buffer.size()) return -1;
        return rx_buffer[rx_pos++];
    }

    int peek() override {
        if (rx_pos >= rx_buffer.size()) return -1;
        return rx_buffer[rx_pos];
    }

    void flush() override {}
    
    void inject_rx(const std::vector<uint8_t>& data) {
        rx_buffer.insert(rx_buffer.end(), data.begin(), data.end());
    }

    void clear() { tx_buffer.clear(); }
    const std::vector<uint8_t>& data() const { return tx_buffer; }
};

class TestFrameBuilder {
public:
    static std::vector<uint8_t> build(uint16_t command_id, const std::vector<uint8_t>& payload) {
        std::vector<uint8_t> frame;
        frame.push_back(rpc::PROTOCOL_VERSION);
        uint16_t len = static_cast<uint16_t>(payload.size());
        frame.push_back((len >> 8) & rpc::RPC_UINT8_MASK);
        frame.push_back(len & rpc::RPC_UINT8_MASK);
        frame.push_back((command_id >> 8) & rpc::RPC_UINT8_MASK);
        frame.push_back(command_id & rpc::RPC_UINT8_MASK);
        frame.insert(frame.end(), payload.begin(), payload.end());
        uint32_t crc = crc32_ieee(frame.data(), frame.size());
        frame.push_back((crc >> 24) & rpc::RPC_UINT8_MASK);
        frame.push_back((crc >> 16) & rpc::RPC_UINT8_MASK);
        frame.push_back((crc >> 8) & rpc::RPC_UINT8_MASK);
        frame.push_back(crc & rpc::RPC_UINT8_MASK);
        std::vector<uint8_t> encoded(frame.size() + 2 + frame.size() / 254 + 1);
        size_t encoded_len = cobs::encode(frame.data(), frame.size(), encoded.data());
        encoded.resize(encoded_len);
        encoded.push_back(rpc::RPC_FRAME_DELIMITER);
        return encoded;
    }
};

void inject_ack(RecordingStream& stream, BridgeClass& bridge, uint16_t command_id) {
    uint16_t ack_cmd_id = static_cast<uint16_t>(rpc::StatusCode::STATUS_ACK);
    std::vector<uint8_t> ack_payload;
    ack_payload.push_back((command_id >> 8) & rpc::RPC_UINT8_MASK);
    ack_payload.push_back(command_id & rpc::RPC_UINT8_MASK);
    std::vector<uint8_t> ack_frame = TestFrameBuilder::build(ack_cmd_id, ack_payload);
    stream.inject_rx(ack_frame);
    bridge.process();
}

void inject_malformed(RecordingStream& stream, BridgeClass& bridge, uint16_t command_id) {
    uint16_t malformed_cmd_id = static_cast<uint16_t>(rpc::StatusCode::STATUS_MALFORMED);
    std::vector<uint8_t> payload;
    payload.push_back((command_id >> 8) & rpc::RPC_UINT8_MASK);
    payload.push_back(command_id & rpc::RPC_UINT8_MASK);
    std::vector<uint8_t> frame = TestFrameBuilder::build(malformed_cmd_id, payload);
    stream.inject_rx(frame);
    bridge.process();
}

// ScopedBridgeBinding using explicit destruction and placement new.
class ScopedBridgeBinding {
 public:
  explicit ScopedBridgeBinding(Stream& stream) {
    TEST_TRACE("ScopedBridgeBinding: Switching to mock stream");
    // Destruct
    Bridge.~BridgeClass();
    
    // Construct
    new (&Bridge) BridgeClass(stream);
    
    // Re-initialize components
    Process.~ProcessClass();
    new (&Process) ProcessClass();

    DataStore.~DataStoreClass();
    new (&DataStore) DataStoreClass();

    Mailbox.~MailboxClass();
    new (&Mailbox) MailboxClass();

    FileSystem.~FileSystemClass();
    new (&FileSystem) FileSystemClass();
    
    Bridge.begin();
    TEST_TRACE("ScopedBridgeBinding: Switch Complete.");
  }

  ~ScopedBridgeBinding() {
    TEST_TRACE("ScopedBridgeBinding: Restoring global serial stream");
    Bridge.~BridgeClass();
    new (&Bridge) BridgeClass(Serial1);
    
    Process.~ProcessClass();
    new (&Process) ProcessClass();

    DataStore.~DataStoreClass();
    new (&DataStore) DataStoreClass();

    Mailbox.~MailboxClass();
    new (&Mailbox) MailboxClass();

    FileSystem.~FileSystemClass();
    new (&FileSystem) FileSystemClass();
    
    Bridge.begin();
    TEST_TRACE("ScopedBridgeBinding: Restore Complete.");
  }

  ScopedBridgeBinding(const ScopedBridgeBinding&) = delete;
  ScopedBridgeBinding& operator=(const ScopedBridgeBinding&) = delete;
};

std::vector<Frame> decode_frames(const std::vector<uint8_t>& bytes) {
  FrameParser parser;
  Frame frame{};
  std::vector<Frame> frames;
  for (uint8_t byte : bytes) {
    bool res = parser.consume(byte, frame);
    if (res) {
      frames.push_back(frame);
    } else if (parser.getError() != FrameParser::Error::NONE) {
        TEST_TRACE("FrameParser Error: " << (int)parser.getError());
    }
  }
  return frames;
}

// TEST CASES

void test_datastore_get_response_dispatches_handler() {
  TEST_TRACE("START: test_datastore_get_response_dispatches_handler");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  DataStore._pending_datastore_head = 0;
  DataStore._pending_datastore_count = 0;
  // FIXED: Use memset for raw arrays
  std::memset(DataStore._pending_datastore_key_lengths, 0, sizeof(DataStore._pending_datastore_key_lengths));
  for (auto& key : DataStore._pending_datastore_keys) {
     std::memset(key, 0, sizeof(key));
  }

  DatastoreHandlerState handler_state;
  DatastoreHandlerState::instance = &handler_state;
  DataStore.onDataStoreGetResponse(datastore_handler_trampoline);

  bool enqueued = DataStore._trackPendingDatastoreKey("thermostat");
  TEST_ASSERT(enqueued);

  Frame frame{};
  frame.header.version = PROTOCOL_VERSION;
  frame.header.command_id = command_value(CommandId::CMD_DATASTORE_GET_RESP);
  frame.header.payload_length = 1 + 5;
  frame.payload[0] = 5;
  std::memcpy(frame.payload + 1, "23.7C", 5);

  Bridge.dispatch(frame);

  TEST_ASSERT(handler_state.called);
  TEST_ASSERT(handler_state.key == "thermostat");
  TEST_ASSERT(handler_state.value == "23.7C");
  DatastoreHandlerState::instance = nullptr;
  TEST_TRACE("PASS: test_datastore_get_response_dispatches_handler");
}

void test_datastore_queue_rejects_overflow() {
  TEST_TRACE("START: test_datastore_queue_rejects_overflow");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  TEST_ASSERT(DataStore._trackPendingDatastoreKey("1"));
  TEST_ASSERT(DataStore._trackPendingDatastoreKey("2"));

  bool overflow = DataStore._trackPendingDatastoreKey("overflow");
  TEST_ASSERT(!overflow);

  const char* key = DataStore._popPendingDatastoreKey();
  TEST_ASSERT(std::strcmp(key, "1") == 0);
  TEST_TRACE("PASS: test_datastore_queue_rejects_overflow");
}

void test_console_write_and_flow_control() {
  TEST_TRACE("START: test_console_write_and_flow_control");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  Console.begin();
  Bridge._synchronized = true; // Manually sync for test

  const uint8_t payload[] = {0xAA, 0xBB, 0xCC};
  size_t written = Console.write(payload, sizeof(payload));
  TEST_ASSERT(written == sizeof(payload));

  auto frames = decode_frames(stream.data()); std::cerr << "[DEBUG] Frames Size: " << frames.size() << std::endl; 
  TEST_ASSERT(frames.size() == 1);
  const Frame& console_frame = frames.front();
  TEST_ASSERT(console_frame.header.command_id == command_value(CommandId::CMD_CONSOLE_WRITE));
  TEST_ASSERT(console_frame.header.payload_length == sizeof(payload));
  TEST_ASSERT(std::memcmp(console_frame.payload, payload, sizeof(payload)) == 0);
  inject_ack(stream, Bridge, command_value(CommandId::CMD_CONSOLE_WRITE));
  stream.clear();

  std::vector<uint8_t> large(MAX_PAYLOAD_SIZE + 5, 0x5A);
  size_t large_written = Console.write(large.data(), large.size());
  TEST_ASSERT(large_written == large.size());
  auto limited_frames = decode_frames(stream.data());
  TEST_ASSERT(limited_frames.size() == 1);
  TEST_ASSERT(limited_frames[0].header.command_id == command_value(CommandId::CMD_CONSOLE_WRITE));
  TEST_ASSERT(limited_frames[0].header.payload_length == MAX_PAYLOAD_SIZE);
  inject_ack(stream, Bridge, command_value(CommandId::CMD_CONSOLE_WRITE));
  stream.clear();

  Bridge.begin();
  Console.begin();
  Bridge._synchronized = true; // Manually sync for test

  std::vector<uint8_t> inbound(ConsoleClass::kBufferHighWater + 2, 0x34);
  Console._push(inbound.data(), inbound.size());
  TEST_ASSERT(Console.available() == static_cast<int>(inbound.size()));
  int peeked = Console.peek();
  TEST_ASSERT(peeked == 0x34);

  auto xoff_frames = decode_frames(stream.data());
  TEST_ASSERT(!xoff_frames.empty());
  TEST_ASSERT(xoff_frames.back().header.command_id == command_value(CommandId::CMD_XOFF));
  inject_ack(stream, Bridge, command_value(CommandId::CMD_XOFF));
  stream.clear();

  for (size_t i = 0; i < inbound.size(); ++i) {
    int value = Console.read();
    TEST_ASSERT(value == 0x34);
  }
  TEST_ASSERT(Console.read() == -1);

  auto xon_frames = decode_frames(stream.data());
  TEST_ASSERT(!xon_frames.empty());
  TEST_ASSERT(xon_frames.back().header.command_id == command_value(CommandId::CMD_XON));
  inject_ack(stream, Bridge, command_value(CommandId::CMD_XON));

  Console.flush();
  TEST_TRACE("PASS: test_console_write_and_flow_control");
}

void test_console_write_blocked_when_not_synced() {
  TEST_TRACE("START: test_console_write_blocked_when_not_synced");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  Bridge.begin();
  Console.begin();

  const uint8_t payload[] = {0xAA, 0xBB, 0xCC};
  size_t written = Console.write(payload, sizeof(payload));
  
  // Should be 0 because sendFrame returns false when not synced
  TEST_ASSERT(written == 0);

  auto frames = decode_frames(stream.data()); std::cerr << "[DEBUG] Frames Size: " << frames.size() << std::endl; 
  TEST_ASSERT(frames.empty());

  TEST_TRACE("PASS: test_console_write_blocked_when_not_synced");
}

void test_datastore_put_and_request_behavior() {
  TEST_TRACE("START: test_datastore_put_and_request_behavior");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  Bridge.begin();
  Bridge._synchronized = true; // Manually sync for test

  const char* key = "temp";
  const char* value = "23.5";

  DataStore.put(key, value);
  auto put_frames = decode_frames(stream.data());
  TEST_ASSERT(put_frames.size() == 1);
  const Frame& put_frame = put_frames.front();
  TEST_ASSERT(put_frame.header.command_id == command_value(CommandId::CMD_DATASTORE_PUT));
  uint8_t key_len = static_cast<uint8_t>(std::strlen(key));
  uint8_t value_len = static_cast<uint8_t>(std::strlen(value));
  TEST_ASSERT(put_frame.payload[0] == key_len);
  TEST_ASSERT(std::memcmp(put_frame.payload + 1, key, key_len) == 0);
  TEST_ASSERT(put_frame.payload[1 + key_len] == value_len);
  TEST_ASSERT(std::memcmp(put_frame.payload + 2 + key_len, value, value_len) == 0);
  inject_ack(stream, Bridge, command_value(CommandId::CMD_DATASTORE_PUT));
  stream.clear();

  DataStore.put(nullptr, value);
  DataStore.put("", value);
  std::string oversized_key(BridgeClass::kMaxDatastoreKeyLength + 1, 'k');
  DataStore.put(oversized_key.c_str(), value);
  std::string oversized_value(BridgeClass::kMaxDatastoreKeyLength + 1, 'v');
  DataStore.put(key, oversized_value.c_str());
  TEST_ASSERT(stream.data().empty());

  DataStore.requestGet(key);
  auto get_frames = decode_frames(stream.data());
  TEST_ASSERT(get_frames.size() == 1);
  TEST_ASSERT(get_frames.front().header.command_id == command_value(CommandId::CMD_DATASTORE_GET));
  inject_ack(stream, Bridge, command_value(CommandId::CMD_DATASTORE_GET));
  stream.clear();

  // Fill the rest of the queue (capacity 4, 1 used)
  DataStore.requestGet("2");
  DataStore.requestGet("3");
  DataStore.requestGet("4");
  stream.clear();

  DataStore.requestGet("overflow");
  auto status_frames = decode_frames(stream.data());
  TEST_ASSERT(!status_frames.empty());
  const Frame& status_frame = status_frames.back();
  TEST_ASSERT(status_frame.header.command_id == status_value(StatusCode::STATUS_ERROR));
  std::string status_message(
      reinterpret_cast<const char*>(status_frame.payload),
      reinterpret_cast<const char*>(status_frame.payload) +
          status_frame.header.payload_length);
  TEST_ASSERT(status_message == "datastore_queue_full");
  (void)DataStore._popPendingDatastoreKey();
  TEST_TRACE("PASS: test_datastore_put_and_request_behavior");
}

void test_mailbox_send_and_requests_emit_commands() {
  TEST_TRACE("START: test_mailbox_send_and_requests_emit_commands");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  Bridge.begin();
  Bridge._synchronized = true; // Manually sync for test

  const char* msg = "hello";
  Mailbox.send(msg);
  auto frames = decode_frames(stream.data()); std::cerr << "[DEBUG] Frames Size: " << frames.size() << std::endl; 
  TEST_ASSERT(frames.size() == 1);
  const Frame& mailbox_frame = frames.front();
  TEST_ASSERT(mailbox_frame.header.command_id == command_value(CommandId::CMD_MAILBOX_PUSH));
  size_t msg_len = std::strlen(msg);
  TEST_ASSERT(mailbox_frame.header.payload_length == msg_len + 2);
  uint16_t encoded_len = read_u16_be(mailbox_frame.payload);
  TEST_ASSERT(encoded_len == msg_len);
  TEST_ASSERT(std::memcmp(mailbox_frame.payload + 2, msg, msg_len) == 0);
  inject_ack(stream, Bridge, command_value(CommandId::CMD_MAILBOX_PUSH));
  stream.clear();

  std::vector<uint8_t> raw(MAX_PAYLOAD_SIZE, 0x41);
  Mailbox.send(raw.data(), raw.size());
  auto raw_frames = decode_frames(stream.data());
  TEST_ASSERT(raw_frames.size() == 1);
  size_t capped_len = MAX_PAYLOAD_SIZE - 2;
  TEST_ASSERT(raw_frames[0].header.payload_length == capped_len + 2);
  uint16_t encoded_raw_len = read_u16_be(raw_frames[0].payload);
  TEST_ASSERT(encoded_raw_len == capped_len);
  inject_ack(stream, Bridge, command_value(CommandId::CMD_MAILBOX_PUSH));
  stream.clear();

  Mailbox.requestRead();
  auto read_frames = decode_frames(stream.data());
  TEST_ASSERT(read_frames.size() == 1);
  TEST_ASSERT(read_frames[0].header.command_id == command_value(CommandId::CMD_MAILBOX_READ));
  TEST_ASSERT(read_frames[0].header.payload_length == 0);
  inject_ack(stream, Bridge, command_value(CommandId::CMD_MAILBOX_READ));
  stream.clear();

  Mailbox.requestAvailable();
  auto avail_frames = decode_frames(stream.data());
  TEST_ASSERT(avail_frames.size() == 1);
  TEST_ASSERT(avail_frames[0].header.command_id == command_value(CommandId::CMD_MAILBOX_AVAILABLE));
  TEST_ASSERT(avail_frames[0].header.payload_length == 0);
  inject_ack(stream, Bridge, command_value(CommandId::CMD_MAILBOX_AVAILABLE));
  stream.clear();
  TEST_TRACE("PASS: test_mailbox_send_and_requests_emit_commands");
}

void test_filesystem_write_and_remove_payloads() {
  TEST_TRACE("START: test_filesystem_write_and_remove_payloads");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  Bridge.begin();
  Bridge._synchronized = true; // Manually sync for test

  const char* path = "/tmp/data";
  std::vector<uint8_t> blob(MAX_PAYLOAD_SIZE, 0xEE);
  FileSystem.write(path, blob.data(), blob.size());
  auto write_frames = decode_frames(stream.data());
  TEST_ASSERT(write_frames.size() == 1);
  const Frame& write_frame = write_frames.front();
  TEST_ASSERT(write_frame.header.command_id == command_value(CommandId::CMD_FILE_WRITE));
  uint8_t path_len = static_cast<uint8_t>(std::strlen(path));
  TEST_ASSERT(write_frame.payload[0] == path_len);
  TEST_ASSERT(std::memcmp(write_frame.payload + 1, path, path_len) == 0);
  uint16_t encoded_len = read_u16_be(write_frame.payload + 1 + path_len);
  size_t max_data = MAX_PAYLOAD_SIZE - 3 - path_len;
  TEST_ASSERT(encoded_len == max_data);
  TEST_ASSERT(write_frame.header.payload_length == path_len + encoded_len + 3);
  inject_ack(stream, Bridge, command_value(CommandId::CMD_FILE_WRITE));
  stream.clear();

  FileSystem.write(nullptr, blob.data(), blob.size());
  FileSystem.write(path, nullptr, blob.size());
  FileSystem.write("", blob.data(), blob.size());
  std::string long_path(300, 'a');
  FileSystem.write(long_path.c_str(), blob.data(), blob.size());
  TEST_ASSERT(stream.data().empty());

  FileSystem.remove(path);
  auto remove_frames = decode_frames(stream.data());
  TEST_ASSERT(remove_frames.size() == 1);
  const Frame& remove_frame = remove_frames.front();
  TEST_ASSERT(remove_frame.header.command_id == command_value(CommandId::CMD_FILE_REMOVE));
  TEST_ASSERT(remove_frame.payload[0] == path_len);
  TEST_ASSERT(std::memcmp(remove_frame.payload + 1, path, path_len) == 0);
  inject_ack(stream, Bridge, command_value(CommandId::CMD_FILE_REMOVE));
  stream.clear();

  FileSystem.remove("");
  TEST_ASSERT(stream.data().empty());
  TEST_TRACE("PASS: test_filesystem_write_and_remove_payloads");
}

void test_process_kill_encodes_pid() {
  TEST_TRACE("START: test_process_kill_encodes_pid");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  Bridge.begin();
  Bridge._synchronized = true; // Manually sync for test

  Process.kill(TEST_CMD_ID);
  auto frames = decode_frames(stream.data()); std::cerr << "[DEBUG] Frames Size: " << frames.size() << std::endl; 
  TEST_ASSERT(frames.size() == 1);
  const Frame& frame = frames.front();
  TEST_ASSERT(frame.header.command_id == command_value(CommandId::CMD_PROCESS_KILL));
  TEST_ASSERT(frame.header.payload_length == 2);
  uint16_t encoded = read_u16_be(frame.payload);
  TEST_ASSERT(encoded == TEST_CMD_ID);
  inject_ack(stream, Bridge, command_value(CommandId::CMD_PROCESS_KILL));
  stream.clear();
  TEST_TRACE("PASS: test_process_kill_encodes_pid");
}

void test_mailbox_read_response_delivers_payload() {
  TEST_TRACE("START: test_mailbox_read_response_delivers_payload");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  MailboxHandlerState mailbox_state;
  MailboxHandlerState::instance = &mailbox_state;
  Mailbox.onMailboxMessage(mailbox_handler_trampoline);

  const char* payload = "hello-linux";
  const uint16_t payload_len = static_cast<uint16_t>(std::strlen(payload));

  Frame frame{};
  frame.header.version = PROTOCOL_VERSION;
  frame.header.command_id = command_value(CommandId::CMD_MAILBOX_READ_RESP);
  frame.header.payload_length = static_cast<uint16_t>(2 + payload_len);
  write_u16_be(frame.payload, payload_len);
  std::memcpy(frame.payload + 2, payload, payload_len);

  Bridge.dispatch(frame);

  TEST_ASSERT(mailbox_state.called);
  TEST_ASSERT(mailbox_state.message == payload);
  MailboxHandlerState::instance = nullptr;
  TEST_TRACE("PASS: test_mailbox_read_response_delivers_payload");
}

void test_process_poll_response_requeues_on_streaming_output() {
  TEST_TRACE("START: test_process_poll_response_requeues_on_streaming_output");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);
  Bridge._synchronized = true; // Manually sync for test

  Process._pending_process_poll_head = 0;
  Process._pending_process_poll_count = 0;
  // Manually clear instead of fill(0) just in case
  // FIXED: Use memset for raw arrays
  std::memset(Process._pending_process_pids, 0, sizeof(Process._pending_process_pids));

  const uint16_t pid = TEST_CMD_ID;
  bool enqueued = Process._pushPendingProcessPid(pid);
  TEST_ASSERT(enqueued);

  ProcessPollHandlerState poll_state;
  ProcessPollHandlerState::instance = &poll_state;
  poll_state.pid_to_requeue = pid;
  Process.onProcessPollResponse(process_poll_handler_trampoline);

  constexpr uint8_t stdout_text[] = {'o', 'k'};

  Frame frame{};
  frame.header.version = PROTOCOL_VERSION;
  frame.header.command_id = command_value(CommandId::CMD_PROCESS_POLL_RESP);
  frame.header.payload_length = 6 + sizeof(stdout_text);
  uint8_t* cursor = frame.payload;
  *cursor++ = status_value(StatusCode::STATUS_OK);
  *cursor++ = TEST_EXIT_CODE;
  write_u16_be(cursor, sizeof(stdout_text));
  cursor += 2;
  std::memcpy(cursor, stdout_text, sizeof(stdout_text));
  cursor += sizeof(stdout_text);
  write_u16_be(cursor, 0);
  cursor += 2;

  stream.clear();
  Bridge.dispatch(frame);

  TEST_ASSERT(poll_state.called);
  TEST_ASSERT(poll_state.status == StatusCode::STATUS_OK);
  TEST_ASSERT(poll_state.exit_code == TEST_EXIT_CODE);
  TEST_ASSERT(poll_state.stdout_text == "ok");
  TEST_ASSERT(poll_state.stderr_text.empty());
  ProcessPollHandlerState::instance = nullptr;

  const auto frames = decode_frames(stream.data()); std::cerr << "[DEBUG] Frames Size: " << frames.size() << std::endl; 
  if(frames.empty()) { std::cerr << "FATAL: No frames decoded!" << std::endl; std::abort(); }
  const Frame& resend = frames.back();
  TEST_ASSERT(resend.header.command_id == command_value(CommandId::CMD_PROCESS_POLL));
  TEST_ASSERT(resend.header.payload_length == 2);
  uint16_t encoded_pid = read_u16_be(resend.payload);
  TEST_ASSERT(encoded_pid == pid);
  
  TEST_TRACE("PASS: test_process_poll_response_requeues_on_streaming_output");
}

void test_begin_preserves_binary_shared_secret_length() {
  TEST_TRACE("START: test_begin_preserves_binary_shared_secret_length");
  RecordingStream stream_explicit;
  BridgeClass bridge_explicit(stream_explicit); // Local

  const uint8_t secret_bytes[] = {0, 0x01, 0x02, 0, 0x03};
  const char* binary_secret = reinterpret_cast<const char*>(secret_bytes);
  bridge_explicit.begin(rpc::RPC_DEFAULT_BAUDRATE, binary_secret, sizeof(secret_bytes));

  assert(
      reinterpret_cast<const void*>(bridge_explicit._shared_secret) ==
      reinterpret_cast<const void*>(binary_secret));
  TEST_ASSERT(bridge_explicit._shared_secret_len == sizeof(secret_bytes));

  const uint8_t nonce[] = {0x10, 0x11, 0x12, 0x13};
  uint8_t explicit_tag[16];
  bridge_explicit._computeHandshakeTag(
      nonce, sizeof(nonce), explicit_tag);

  bool explicit_has_entropy = false;
  for (uint8_t byte : explicit_tag) {
    if (byte != 0) {
      explicit_has_entropy = true;
      break;
    }
  }
  TEST_ASSERT(explicit_has_entropy);

  RecordingStream stream_default;
  BridgeClass bridge_default(stream_default); // Local
  bridge_default.begin(rpc::RPC_DEFAULT_BAUDRATE, binary_secret);

  uint8_t truncated_tag[16];
  bridge_default._computeHandshakeTag(
      nonce, sizeof(nonce), truncated_tag);

  bool truncated_all_zero = true;
  for (uint8_t byte : truncated_tag) {
    if (byte != 0) {
      truncated_all_zero = false;
      break;
    }
  }
  TEST_ASSERT(truncated_all_zero);
  TEST_ASSERT(std::memcmp(explicit_tag, truncated_tag, sizeof(explicit_tag)) != 0);
  TEST_TRACE("PASS: test_begin_preserves_binary_shared_secret_length");
}

void test_ack_flushes_pending_queue_after_response() {
  TEST_TRACE("START: test_ack_flushes_pending_queue_after_response");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  Bridge.begin();
  Bridge._synchronized = true; // Manually sync for test

  const uint8_t first_payload[] = {0x42};
  bool sent = Bridge.sendFrame(
      CommandId::CMD_CONSOLE_WRITE,
      first_payload, sizeof(first_payload));
  TEST_ASSERT(sent);
  TEST_ASSERT(Bridge._awaiting_ack);

  const uint8_t queued_payload[] = {0xAA, 0xBB};
    bool enqueued = Bridge._enqueuePendingTx(
      command_value(CommandId::CMD_MAILBOX_PUSH),
      queued_payload, sizeof(queued_payload));
  TEST_ASSERT(enqueued);
  TEST_ASSERT(Bridge._pending_tx_count == 1);

  auto before = decode_frames(stream.data());
  size_t before_count = before.size();

  inject_ack(stream, Bridge, command_value(CommandId::CMD_CONSOLE_WRITE));

  auto after = decode_frames(stream.data());
  TEST_ASSERT(after.size() == before_count + 1);
  const Frame& flushed = after.back();
  TEST_ASSERT(flushed.header.command_id == command_value(CommandId::CMD_MAILBOX_PUSH));
  TEST_ASSERT(flushed.header.payload_length == sizeof(queued_payload));
  TEST_ASSERT(std::memcmp(flushed.payload, queued_payload, sizeof(queued_payload)) == 0);
  TEST_ASSERT(Bridge._pending_tx_count == 0);
  TEST_ASSERT(Bridge._awaiting_ack);
  TEST_TRACE("PASS: test_ack_flushes_pending_queue_after_response");
}

void test_status_ack_frame_clears_pending_state_via_dispatch() {
  TEST_TRACE("START: test_status_ack_frame_clears_pending_state_via_dispatch");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  Bridge.begin();
  Bridge._synchronized = true; // Manually sync for test
  StatusHandlerState status_state;
  StatusHandlerState::instance = &status_state;
  Bridge.onStatus(status_handler_trampoline);

  const uint8_t payload[] = {0x55};
  bool sent = Bridge.sendFrame(
      CommandId::CMD_CONSOLE_WRITE, payload, sizeof(payload));
  TEST_ASSERT(sent);
  TEST_ASSERT(Bridge._awaiting_ack);

  Frame ack{};
  ack.header.version = PROTOCOL_VERSION;
  ack.header.command_id = status_value(StatusCode::STATUS_ACK);
  ack.header.payload_length = 2;
  write_u16_be(ack.payload, command_value(CommandId::CMD_CONSOLE_WRITE));

  Bridge.dispatch(ack);

  TEST_ASSERT(!Bridge._awaiting_ack);
  TEST_ASSERT(status_state.called);
  TEST_ASSERT(status_state.status_code == StatusCode::STATUS_ACK);
  StatusHandlerState::instance = nullptr;
  TEST_TRACE("PASS: test_status_ack_frame_clears_pending_state_via_dispatch");
}

void test_status_error_frame_dispatches_handler() {
  TEST_TRACE("START: test_status_error_frame_dispatches_handler");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  StatusHandlerState status_state;
  StatusHandlerState::instance = &status_state;
  Bridge.onStatus(status_handler_trampoline);

  const char* message = "remote_fault";
  Frame frame{};
  frame.header.version = PROTOCOL_VERSION;
  frame.header.command_id = status_value(StatusCode::STATUS_ERROR);
  frame.header.payload_length = static_cast<uint16_t>(std::strlen(message));
  std::memcpy(frame.payload, message, frame.header.payload_length);

  Bridge.dispatch(frame);

  TEST_ASSERT(status_state.called);
  TEST_ASSERT(status_state.status_code == StatusCode::STATUS_ERROR);
  TEST_ASSERT(status_state.payload == message);
  StatusHandlerState::instance = nullptr;
  TEST_TRACE("PASS: test_status_error_frame_dispatches_handler");
}

void test_serial_overflow_emits_status_notification() {
  TEST_TRACE("START: test_serial_overflow_emits_status_notification");
  std::vector<uint8_t> oversized(rpc::COBS_BUFFER_SIZE + 8, 0xAA);
  RecordingStream stream;
  stream.inject_rx(oversized);
  ScopedBridgeBinding binding(stream);

  StatusHandlerState status_state;
  StatusHandlerState::instance = &status_state;
  Bridge.onStatus(status_handler_trampoline);

  Bridge.process();

  TEST_ASSERT(status_state.called);
  TEST_ASSERT(status_state.status_code == StatusCode::STATUS_MALFORMED);
  TEST_ASSERT(status_state.payload == "serial_rx_overflow");
  StatusHandlerState::instance = nullptr;
  TEST_TRACE("PASS: test_serial_overflow_emits_status_notification");
}

void test_malformed_status_triggers_retransmit() {
  TEST_TRACE("START: test_malformed_status_triggers_retransmit");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  Bridge.begin();
  Bridge._synchronized = true; // Manually sync for test

  const uint8_t payload[] = {0x10, 0x20, 0x30};
  bool sent = Bridge.sendFrame(
      CommandId::CMD_MAILBOX_PUSH, payload, sizeof(payload));
  TEST_ASSERT(sent);
  TEST_ASSERT(Bridge._awaiting_ack);

  auto before = decode_frames(stream.data());
  TEST_ASSERT(before.size() == 1);

  inject_malformed(stream, Bridge, command_value(CommandId::CMD_MAILBOX_PUSH));

  auto after = decode_frames(stream.data());
  TEST_ASSERT(after.size() == 2);
  const Frame& resent = after.back();
  TEST_ASSERT(resent.header.command_id == command_value(CommandId::CMD_MAILBOX_PUSH));
  TEST_ASSERT(resent.header.payload_length == sizeof(payload));
  TEST_ASSERT(std::memcmp(resent.payload, payload, sizeof(payload)) == 0);
  TEST_ASSERT(Bridge._retry_count == 1);
  TEST_TRACE("PASS: test_malformed_status_triggers_retransmit");
}

void test_link_sync_generates_tag_and_ack() {
  TEST_TRACE("START: test_link_sync_generates_tag_and_ack");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  const char* secret = "unit-test-secret";
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, secret);

  const uint8_t nonce[RPC_HANDSHAKE_NONCE_LENGTH] = {
      0x01, 0x02, 0x03, 0x04,
      0x05, 0x06, 0x07, 0x08,
      0x09, 0x0A, 0x0B, 0x0C,
      0x0D, 0x0E, 0x0F, 0x10};
  Frame frame{};
  frame.header.version = PROTOCOL_VERSION;
  frame.header.command_id = command_value(CommandId::CMD_LINK_SYNC);
  frame.header.payload_length = sizeof(nonce);
  std::memcpy(frame.payload, nonce, sizeof(nonce));

  stream.clear();
  Bridge.dispatch(frame);

  auto frames = decode_frames(stream.data()); std::cerr << "[DEBUG] Frames Size: " << frames.size() << std::endl; 
  TEST_ASSERT(frames.size() == 2);
  const Frame& sync = frames.front();
  TEST_ASSERT(sync.header.command_id == command_value(CommandId::CMD_LINK_SYNC_RESP));
  TEST_ASSERT(sync.header.payload_length == sizeof(nonce) + 16);
  TEST_ASSERT(std::memcmp(sync.payload, nonce, sizeof(nonce)) == 0);
  uint8_t expected_tag[16];
  Bridge._computeHandshakeTag(nonce, sizeof(nonce), expected_tag);
  TEST_ASSERT(std::memcmp(sync.payload + sizeof(nonce), expected_tag, 16) == 0);

  const Frame& ack = frames.back();
  TEST_ASSERT(ack.header.command_id == status_value(StatusCode::STATUS_ACK));
  TEST_ASSERT(ack.header.payload_length == 2);
  TEST_ASSERT(read_u16_be(ack.payload) == command_value(CommandId::CMD_LINK_SYNC));
  TEST_TRACE("PASS: test_link_sync_generates_tag_and_ack");
}

void test_link_sync_without_secret_replays_nonce_only() {
  TEST_TRACE("START: test_link_sync_without_secret_replays_nonce_only");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, nullptr);

  const uint8_t nonce[RPC_HANDSHAKE_NONCE_LENGTH] = {
      0xAA, 0xBB, 0xCC, 0xDD,
      0xEE, 0x01, 0x02, 0x03,
      0x04, 0x05, 0x06, 0x07,
      0x08, 0x09, 0x0A, 0x0B};
  Frame frame{};
  frame.header.version = PROTOCOL_VERSION;
  frame.header.command_id = command_value(CommandId::CMD_LINK_SYNC);
  frame.header.payload_length = sizeof(nonce);
  std::memcpy(frame.payload, nonce, sizeof(nonce));

  stream.clear();
  Bridge.dispatch(frame);

  auto frames = decode_frames(stream.data()); std::cerr << "[DEBUG] Frames Size: " << frames.size() << std::endl; 
  TEST_ASSERT(frames.size() == 2);
  const Frame& sync = frames.front();
  TEST_ASSERT(sync.header.command_id == command_value(CommandId::CMD_LINK_SYNC_RESP));
  TEST_ASSERT(sync.header.payload_length == sizeof(nonce));
  TEST_ASSERT(std::memcmp(sync.payload, nonce, sizeof(nonce)) == 0);
  TEST_TRACE("PASS: test_link_sync_without_secret_replays_nonce_only");
}

void test_ack_timeout_emits_status_and_resets_state() {
  TEST_TRACE("START: test_ack_timeout_emits_status_and_resets_state");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  Bridge.begin();
  Bridge._synchronized = true; // Manually sync for test
  StatusHandlerState status_state;
  StatusHandlerState::instance = &status_state;
  Bridge.onStatus(status_handler_trampoline);

  const uint8_t payload[] = {0x99};
  bool sent = Bridge.sendFrame(
      CommandId::CMD_MAILBOX_PUSH, payload, sizeof(payload));
  TEST_ASSERT(sent);
  TEST_ASSERT(Bridge._awaiting_ack);

  Bridge._retry_count = BridgeClass::kMaxAckRetries;
  Bridge._last_send_millis = 1000;
  Bridge._processAckTimeout();

  TEST_ASSERT(status_state.called);
  TEST_ASSERT(status_state.status_code == StatusCode::STATUS_TIMEOUT);
  TEST_ASSERT(!Bridge._awaiting_ack);
  StatusHandlerState::instance = nullptr;
  TEST_TRACE("PASS: test_ack_timeout_emits_status_and_resets_state");
}

void test_process_run_rejects_oversized_payload() {
  TEST_TRACE("START: test_process_run_rejects_oversized_payload");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  StatusHandlerState status_state;
  StatusHandlerState::instance = &status_state;
  Bridge.onStatus(status_handler_trampoline);

  std::string huge(rpc::MAX_PAYLOAD_SIZE + 4, 'x');
  Process.run(huge.c_str());

  auto frames = decode_frames(stream.data()); std::cerr << "[DEBUG] Frames Size: " << frames.size() << std::endl; 
  TEST_ASSERT(frames.size() == 1);
  const Frame& status_frame = frames.front();
  TEST_ASSERT(status_frame.header.command_id == status_value(StatusCode::STATUS_ERROR));
  std::string message(
      reinterpret_cast<const char*>(status_frame.payload),
      reinterpret_cast<const char*>(status_frame.payload) +
          status_frame.header.payload_length);
  TEST_ASSERT(message == "process_run_payload_too_large");
  TEST_ASSERT(status_state.called);
  TEST_ASSERT(status_state.payload == "process_run_payload_too_large");

  inject_ack(stream, Bridge, status_value(StatusCode::STATUS_ERROR));
  stream.clear();
  StatusHandlerState::instance = nullptr;
  TEST_TRACE("PASS: test_process_run_rejects_oversized_payload");
}

void test_bridge_process_run_success() {
  TEST_TRACE("START: test_bridge_process_run_success");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  Bridge.begin();
  Bridge._synchronized = true; // Manually sync for test

  const char* command = "ls -la";
  Process.run(command);

  auto frames = decode_frames(stream.data()); std::cerr << "[DEBUG] Frames Size: " << frames.size() << std::endl; 
  TEST_ASSERT(frames.size() == 1);
  const Frame& frame = frames.front();
  TEST_ASSERT(frame.header.command_id == command_value(CommandId::CMD_PROCESS_RUN));
  TEST_ASSERT(frame.header.payload_length == std::strlen(command));
  TEST_ASSERT(std::memcmp(frame.payload, command, std::strlen(command)) == 0);
  
  inject_ack(stream, Bridge, command_value(CommandId::CMD_PROCESS_RUN));
  stream.clear();
  TEST_TRACE("PASS: test_bridge_process_run_success");
}

void test_apply_timing_config_accepts_valid_payload() {
  TEST_TRACE("START: test_apply_timing_config_accepts_valid_payload");
  RecordingStream stream;
  BridgeClass bridge(stream); // Local instance

  uint8_t payload[RPC_HANDSHAKE_CONFIG_SIZE] = {};
  const uint16_t ack_timeout = RPC_HANDSHAKE_ACK_TIMEOUT_MIN_MS + 5;
  const uint8_t retry_limit = RPC_HANDSHAKE_RETRY_LIMIT_MIN + 1;
  const uint32_t response_timeout =
      RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS - 250;

  rpc::write_u16_be(payload, ack_timeout);
  payload[2] = retry_limit;
  rpc::write_u32_be(payload + 3, response_timeout);

  bridge._applyTimingConfig(payload, sizeof(payload));

  TEST_ASSERT(bridge._ack_timeout_ms == ack_timeout);
  TEST_ASSERT(bridge._ack_retry_limit == retry_limit);
  TEST_ASSERT(bridge._response_timeout_ms == response_timeout);
  TEST_TRACE("PASS: test_apply_timing_config_accepts_valid_payload");
}

void test_apply_timing_config_rejects_invalid_payload() {
  TEST_TRACE("START: test_apply_timing_config_rejects_invalid_payload");
  RecordingStream stream;
  BridgeClass bridge(stream); // Local instance

  uint8_t payload[RPC_HANDSHAKE_CONFIG_SIZE] = {};
  const uint16_t invalid_ack_timeout = RPC_HANDSHAKE_ACK_TIMEOUT_MAX_MS + 5;
  const uint8_t invalid_retry_limit = RPC_HANDSHAKE_RETRY_LIMIT_MAX + 1;
  const uint32_t invalid_response_timeout =
      RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS + 1;

  rpc::write_u16_be(payload, invalid_ack_timeout);
  payload[2] = invalid_retry_limit;
  rpc::write_u32_be(payload + 3, invalid_response_timeout);

  bridge._applyTimingConfig(payload, sizeof(payload));

  TEST_ASSERT(bridge._ack_timeout_ms == BridgeClass::kAckTimeoutMs);
  TEST_ASSERT(bridge._ack_retry_limit == BridgeClass::kMaxAckRetries);
  TEST_ASSERT(bridge._response_timeout_ms == RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS);

  bridge._ack_timeout_ms = 1;
  bridge._ack_retry_limit = 1;
  bridge._response_timeout_ms = RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS;
  bridge._applyTimingConfig(payload, RPC_HANDSHAKE_CONFIG_SIZE - 1);

  TEST_ASSERT(bridge._ack_timeout_ms == BridgeClass::kAckTimeoutMs);
  TEST_ASSERT(bridge._ack_retry_limit == BridgeClass::kMaxAckRetries);
  TEST_ASSERT(bridge._response_timeout_ms == RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS);
  TEST_TRACE("PASS: test_apply_timing_config_rejects_invalid_payload");
}


struct FileReadState {
  static FileReadState* instance;
  bool called = false;
  std::vector<uint8_t> data;
};
FileReadState* FileReadState::instance = nullptr;

void test_filesystem_handle_read_response() {
  TEST_TRACE("START: test_filesystem_handle_read_response");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  FileReadState state;
  FileReadState::instance = &state;

  auto handler = [](const uint8_t* data, uint16_t len) {
    if (FileReadState::instance) {
      FileReadState::instance->called = true;
      FileReadState::instance->data.assign(data, data + len);
    }
  };

  FileSystem.onFileSystemReadResponse(handler);

  const char* content = "file content";
  uint16_t content_len = strlen(content);
  
  // payload: [len_hi, len_lo, ...data...]
  std::vector<uint8_t> payload;
  payload.push_back((content_len >> 8) & rpc::RPC_UINT8_MASK);
  payload.push_back(content_len & rpc::RPC_UINT8_MASK);
  payload.insert(payload.end(), content, content + content_len);

  Frame frame{};
  frame.header.version = PROTOCOL_VERSION;
  frame.header.command_id = command_value(CommandId::CMD_FILE_READ_RESP);
  frame.header.payload_length = payload.size();
  std::memcpy(frame.payload, payload.data(), payload.size());

  Bridge.dispatch(frame);

  TEST_ASSERT(state.called);
  TEST_ASSERT(state.data.size() == content_len);
  TEST_ASSERT(std::memcmp(state.data.data(), content, content_len) == 0);
  
  FileReadState::instance = nullptr;
  TEST_TRACE("PASS: test_filesystem_handle_read_response");
}

void test_filesystem_handle_write_request() {
  TEST_TRACE("START: test_filesystem_handle_write_request");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  // Simulate an incoming CMD_FILE_WRITE.
  // Although on host we don't write to EEPROM, we should ensure the code path is parsing correctly.
  // The implementation checks for "/eeprom/" prefix.
  
  const char* path = "/eeprom/10";
  const char* data = "val";
  uint8_t path_len = strlen(path);
  uint16_t data_len = strlen(data);

  std::vector<uint8_t> payload;
  payload.push_back(path_len);
  payload.insert(payload.end(), path, path + path_len);
  payload.insert(payload.end(), data, data + data_len);

  Frame frame{};
  frame.header.version = PROTOCOL_VERSION;
  frame.header.command_id = command_value(CommandId::CMD_FILE_WRITE);
  frame.header.payload_length = payload.size();
  std::memcpy(frame.payload, payload.data(), payload.size());

  // Dispatch should process it without crashing or erroring
  Bridge.dispatch(frame);

  // Test with invalid payload (short)
  frame.header.payload_length = 1;
  Bridge.dispatch(frame);

  TEST_TRACE("PASS: test_filesystem_handle_write_request");
}

struct ProcessState {
  static ProcessState* instance;
  bool run_called = false;
  bool async_called = false;
  bool poll_called = false;
  int async_pid = -1;
};
ProcessState* ProcessState::instance = nullptr;

void test_process_methods() {
  TEST_TRACE("START: test_process_methods");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  Bridge.begin();
  Bridge._synchronized = true; // Manually sync for test

  ProcessState state;
  ProcessState::instance = &state;

  Process.onProcessRunResponse([](StatusCode, const uint8_t*, uint16_t, const uint8_t*, uint16_t) {
    if (ProcessState::instance) ProcessState::instance->run_called = true;
  });
  Process.onProcessRunAsyncResponse([](int pid) {
    if (ProcessState::instance) {
      ProcessState::instance->async_called = true;
      ProcessState::instance->async_pid = pid;
    }
  });
  Process.onProcessPollResponse([](StatusCode, uint8_t, const uint8_t*, uint16_t, const uint8_t*, uint16_t) {
    if (ProcessState::instance) ProcessState::instance->poll_called = true;
  });

  // 1. runAsync success
  const char* cmd = "sleep 1";
  Process.runAsync(cmd);
  TEST_TRACE("After runAsync. Stream size: " << stream.data().size());
  TEST_TRACE("Bridge synchronized: " << Bridge.isSynchronized());
  auto frames = decode_frames(stream.data()); std::cerr << "[DEBUG] Frames Size: " << frames.size() << std::endl; 
  TEST_TRACE("Frames size: " << frames.size());
  if(frames.empty()) { std::cerr << "FATAL: No frames decoded!" << std::endl; std::abort(); }
  TEST_ASSERT(frames.back().header.command_id == command_value(CommandId::CMD_PROCESS_RUN_ASYNC));
  inject_ack(stream, Bridge, command_value(CommandId::CMD_PROCESS_RUN_ASYNC));
  stream.clear();

  // 2. run oversized
  std::string huge(MAX_PAYLOAD_SIZE + 5, 'a');
  Process.run(huge.c_str());
  // Expect STATUS_ERROR
  frames = decode_frames(stream.data());
  TEST_TRACE("Step 2 frames: " << frames.size());
  if(frames.empty()) { std::cerr << "FATAL: No frames decoded!" << std::endl; std::abort(); }
  TEST_ASSERT(frames.back().header.command_id == status_value(StatusCode::STATUS_ERROR));
  stream.clear();

  // 3. runAsync oversized
  Process.runAsync(huge.c_str());
  frames = decode_frames(stream.data());
  TEST_TRACE("Step 3 frames: " << frames.size());
  if(frames.empty()) { std::cerr << "FATAL: No frames decoded!" << std::endl; std::abort(); }
  TEST_ASSERT(frames.back().header.command_id == status_value(StatusCode::STATUS_ERROR));
  stream.clear();

  // 4. poll queue full
  // Capacity is 2
  Process.poll(10);
  Process.poll(11);
  stream.clear();
  Process.poll(12); // Should fail
  frames = decode_frames(stream.data());
  if(frames.empty()) { std::cerr << "FATAL: No frames decoded!" << std::endl; std::abort(); }
  TEST_ASSERT(frames.back().header.command_id == status_value(StatusCode::STATUS_ERROR));
  stream.clear();

  // 5. handleResponse dispatch
  // CMD_PROCESS_RUN_RESP
  Frame resp{};
  resp.header.version = PROTOCOL_VERSION;
  resp.header.command_id = command_value(CommandId::CMD_PROCESS_RUN_RESP);
  resp.header.payload_length = 5; 
  // status(1) + stdout_len(2) + stderr_len(2) -> minimal
  resp.payload[0] = status_value(StatusCode::STATUS_OK);
  write_u16_be(resp.payload+1, 0); // stdout len
  write_u16_be(resp.payload+3, 0); // stderr len
  Bridge.dispatch(resp);
  TEST_ASSERT(state.run_called);

  // CMD_PROCESS_RUN_ASYNC_RESP
  resp.header.command_id = command_value(CommandId::CMD_PROCESS_RUN_ASYNC_RESP);
  resp.header.payload_length = 2;
  write_u16_be(resp.payload, 123);
  Bridge.dispatch(resp);
  TEST_ASSERT(state.async_called);
  TEST_ASSERT(state.async_pid == 123);

  // CMD_PROCESS_POLL_RESP
  // Need to clear pending pid to receive poll response? 
  // We filled the queue earlier (10, 11). So it expects a response for 10.
  resp.header.command_id = command_value(CommandId::CMD_PROCESS_POLL_RESP);
  resp.header.payload_length = 6;
  // status(1) + running(1) + stdout_len(2) + stderr_len(2)
  resp.payload[0] = status_value(StatusCode::STATUS_OK);
  resp.payload[1] = 1; // running
  write_u16_be(resp.payload+2, 0);
  write_u16_be(resp.payload+4, 0);
  Bridge.dispatch(resp);
  TEST_ASSERT(state.poll_called);

  ProcessState::instance = nullptr;
  TEST_TRACE("PASS: test_process_methods");
}

struct MailboxState {
  static MailboxState* instance;
  bool msg_called = false;
  bool avail_called = false;
  uint8_t avail_count = 0;
};
MailboxState* MailboxState::instance = nullptr;

void test_mailbox_methods() {
  TEST_TRACE("START: test_mailbox_methods");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  MailboxState state;
  MailboxState::instance = &state;

  Mailbox.onMailboxMessage([](const uint8_t*, uint16_t){
    if (MailboxState::instance) MailboxState::instance->msg_called = true;
  });
  Mailbox.onMailboxAvailableResponse([](uint16_t c){
    if (MailboxState::instance) {
      MailboxState::instance->avail_called = true;
      MailboxState::instance->avail_count = (uint8_t)c;
    }
  });

  // 1. send empty
  Mailbox.send((const char*)nullptr);
  Mailbox.send("");
  Mailbox.send((const uint8_t*)nullptr, 10);
  Mailbox.send((const uint8_t*)"data", 0);
  TEST_ASSERT(stream.data().empty());

  // 2. CMD_MAILBOX_AVAILABLE_RESP
  Frame resp{};
  resp.header.version = PROTOCOL_VERSION;
  resp.header.command_id = command_value(CommandId::CMD_MAILBOX_AVAILABLE_RESP);
  resp.header.payload_length = 1;
  resp.payload[0] = 5;
  Bridge.dispatch(resp);
  TEST_ASSERT(state.avail_called);
  TEST_ASSERT(state.avail_count == 5);
  state.avail_called = false;

  // 3. CMD_MAILBOX_PUSH (Incoming)
  resp.header.command_id = command_value(CommandId::CMD_MAILBOX_PUSH);
  resp.header.payload_length = 3;
  write_u16_be(resp.payload, 1);
  resp.payload[2] = 'A';
  Bridge.dispatch(resp);
  TEST_ASSERT(state.msg_called);
  state.msg_called = false;

  // 4. CMD_MAILBOX_AVAILABLE (Incoming)
  resp.header.command_id = command_value(CommandId::CMD_MAILBOX_AVAILABLE);
  resp.header.payload_length = 1;
  resp.payload[0] = 3;
  Bridge.dispatch(resp);
  TEST_ASSERT(state.avail_called);
  TEST_ASSERT(state.avail_count == 3);

  MailboxState::instance = nullptr;
  TEST_TRACE("PASS: test_mailbox_methods");
}

void test_bridge_hardware_serial_constructor() {
  TEST_TRACE("START: test_bridge_hardware_serial_constructor");
  // Instantiate Bridge with HardwareSerial to cover that constructor
  BridgeClass hwBridge(Serial);
  // We can't really do much with it without interfering with the global instance if we were running on a real board,
  // but here it's just a test instance.
  // Just verifying it constructs and destructs.
  TEST_TRACE("PASS: test_bridge_hardware_serial_constructor");
}

void test_system_commands() {
  TEST_TRACE("START: test_system_commands");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);
  Bridge.begin();
  Bridge._synchronized = true;

  // CMD_GET_VERSION
  Frame frame{};
  frame.header.version = PROTOCOL_VERSION;
  frame.header.command_id = command_value(CommandId::CMD_GET_VERSION);
  frame.header.payload_length = 0;
  Bridge.dispatch(frame);
  
  TEST_TRACE("After dispatch GET_VERSION. Stream size: " << stream.data().size());
  std::cout << "Stream data: ";
  for (uint8_t b : stream.data()) {
      std::cout << std::hex << (int)b << " ";
  }
  std::cout << std::dec << std::endl;
  
  auto frames = decode_frames(stream.data()); std::cerr << "[DEBUG] Frames Size: " << frames.size() << std::endl; 
  if(frames.empty()) { std::cerr << "FATAL: No frames decoded!" << std::endl; std::abort(); }
  TEST_ASSERT(frames.back().header.command_id == command_value(CommandId::CMD_GET_VERSION_RESP));
  TEST_ASSERT(frames.back().header.payload_length == 2);
  TEST_ASSERT(frames.back().payload[0] == BridgeClass::kFirmwareVersionMajor);
  TEST_ASSERT(frames.back().payload[1] == BridgeClass::kFirmwareVersionMinor);
  
  // Inject ACK for GET_VERSION_RESP
  inject_ack(stream, Bridge, command_value(CommandId::CMD_GET_VERSION_RESP));
  stream.clear();

  // CMD_GET_FREE_MEMORY
  frame.header.command_id = command_value(CommandId::CMD_GET_FREE_MEMORY);
  Bridge.dispatch(frame);
  frames = decode_frames(stream.data());
  if(frames.empty()) { std::cerr << "FATAL: No frames decoded!" << std::endl; std::abort(); }
  TEST_ASSERT(frames.back().header.command_id == command_value(CommandId::CMD_GET_FREE_MEMORY_RESP));
  TEST_ASSERT(frames.back().header.payload_length == 2);
  // Expect 0 in host test
  TEST_ASSERT(frames.back().payload[0] == 0);
  TEST_ASSERT(frames.back().payload[1] == 0);
  
  // Inject ACK for GET_FREE_MEMORY_RESP
  inject_ack(stream, Bridge, command_value(CommandId::CMD_GET_FREE_MEMORY_RESP));
  stream.clear();
  
  // CMD_GET_TX_DEBUG_SNAPSHOT
  frame.header.command_id = command_value(CommandId::CMD_GET_TX_DEBUG_SNAPSHOT);
  Bridge.dispatch(frame);
  frames = decode_frames(stream.data());
  if(frames.empty()) { std::cerr << "FATAL: No frames decoded!" << std::endl; std::abort(); }
  TEST_ASSERT(frames.back().header.command_id == command_value(CommandId::CMD_GET_TX_DEBUG_SNAPSHOT_RESP));
  
  // Inject ACK for GET_TX_DEBUG_SNAPSHOT_RESP
  inject_ack(stream, Bridge, command_value(CommandId::CMD_GET_TX_DEBUG_SNAPSHOT_RESP));
  stream.clear();
  
  // CMD_SET_BAUDRATE
  frame.header.command_id = command_value(CommandId::CMD_SET_BAUDRATE);
  frame.header.payload_length = 4;
  write_u32_be(frame.payload, 57600);
  Bridge.dispatch(frame);
  frames = decode_frames(stream.data());
  if(frames.empty()) { std::cerr << "FATAL: No frames decoded!" << std::endl; std::abort(); }
  TEST_ASSERT(frames.back().header.command_id == command_value(CommandId::CMD_SET_BAUDRATE_RESP));
  
  // Inject ACK for SET_BAUDRATE_RESP
  inject_ack(stream, Bridge, command_value(CommandId::CMD_SET_BAUDRATE_RESP));
  stream.clear();
  
  TEST_TRACE("PASS: test_system_commands");
}

void test_bridge_process_input_errors() {
  TEST_TRACE("START: test_bridge_process_input_errors");
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);
  Bridge.begin();

  // Construct a valid frame
  std::vector<uint8_t> frame_data;
  frame_data.push_back(PROTOCOL_VERSION);
  frame_data.push_back(0); frame_data.push_back(0); // Len 0
  frame_data.push_back(0); frame_data.push_back(rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION)); // CMD_GET_VERSION
  
  // Add Bad CRC
  frame_data.push_back(0xDE);
  frame_data.push_back(0xAD);
  frame_data.push_back(0xBE);
  frame_data.push_back(0xEF);
  
  // Encode COBS
  std::vector<uint8_t> cobs_data(frame_data.size() + 5); 
  size_t cobs_len = cobs::encode(frame_data.data(), frame_data.size(), cobs_data.data());
  cobs_data.resize(cobs_len);
  cobs_data.push_back(rpc::RPC_FRAME_DELIMITER); // Delimiter
  
  stream.inject_rx(cobs_data);
  
  // Process
  Bridge.process();
  
  // Check output frame for STATUS_CRC_MISMATCH
  auto frames = decode_frames(stream.data()); std::cerr << "[DEBUG] Frames Size: " << frames.size() << std::endl; 
  if(frames.empty()) { std::cerr << "FATAL: No frames decoded!" << std::endl; std::abort(); }
  TEST_ASSERT(frames.back().header.command_id == status_value(StatusCode::STATUS_CRC_MISMATCH));
  stream.clear();
  
  TEST_TRACE("PASS: test_bridge_process_input_errors");
}

}  // namespace

int main() {
  TEST_TRACE("Starting Main Tests");
  test_datastore_get_response_dispatches_handler();
  test_datastore_queue_rejects_overflow();
  test_console_write_and_flow_control();
  test_console_write_blocked_when_not_synced();
  test_datastore_put_and_request_behavior();
  test_mailbox_send_and_requests_emit_commands();
  test_filesystem_write_and_remove_payloads();
  test_filesystem_handle_read_response();
  test_filesystem_handle_write_request();
  test_process_kill_encodes_pid();
  test_mailbox_read_response_delivers_payload();
  test_process_poll_response_requeues_on_streaming_output();
  test_begin_preserves_binary_shared_secret_length();
  test_ack_flushes_pending_queue_after_response();
  test_status_ack_frame_clears_pending_state_via_dispatch();
  test_status_error_frame_dispatches_handler();
  test_serial_overflow_emits_status_notification();
  test_malformed_status_triggers_retransmit();
  test_link_sync_generates_tag_and_ack();
  test_link_sync_without_secret_replays_nonce_only();
  test_ack_timeout_emits_status_and_resets_state();
  test_process_run_rejects_oversized_payload();
  test_bridge_process_run_success();
  test_apply_timing_config_accepts_valid_payload();
  test_apply_timing_config_rejects_invalid_payload();
  test_process_methods();
  test_mailbox_methods();
  test_bridge_hardware_serial_constructor();
  test_system_commands();
  test_bridge_process_input_errors();
  TEST_TRACE("All tests passed");
  return 0;
}