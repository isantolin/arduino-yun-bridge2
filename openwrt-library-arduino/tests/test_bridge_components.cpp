#include <cassert>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <new>
#include <string>
#include <vector>

#define private public
#define protected public
#include "Bridge.h"
#undef private
#undef protected

#include "protocol/rpc_protocol.h"

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
    const char* key, const uint8_t* value, uint8_t length) {
  auto* state = DatastoreHandlerState::instance;
  if (!state) {
    return;
  }
  state->called = true;
  state->key = key ? key : "";
  state->value.assign(
      reinterpret_cast<const char*>(value),
      reinterpret_cast<const char*>(value) + length);
}

struct MailboxHandlerState {
  static MailboxHandlerState* instance;
  bool called = false;
  std::string message;
};

MailboxHandlerState* MailboxHandlerState::instance = nullptr;

void mailbox_handler_trampoline(const uint8_t* buffer, size_t size) {
  auto* state = MailboxHandlerState::instance;
  if (!state) {
    return;
  }
  state->called = true;
  state->message.assign(reinterpret_cast<const char*>(buffer), size);
}

struct ProcessPollHandlerState {
  static ProcessPollHandlerState* instance;
  bool called = false;
  StatusCode status = StatusCode::STATUS_ERROR;
  uint8_t exit_code = 0xFF;
  std::string stdout_text;
  std::string stderr_text;
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
  if (!state) {
    return;
  }
  state->called = true;
  state->status = status;
  state->exit_code = exit_code;
  state->stdout_text.assign(
      reinterpret_cast<const char*>(stdout_data), stdout_len);
  state->stderr_text.assign(
      reinterpret_cast<const char*>(stderr_data), stderr_len);
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
  if (!state) {
    return;
  }
  state->called = true;
  state->status_code = status_code;
  state->payload.assign(
      reinterpret_cast<const char*>(payload),
      reinterpret_cast<const char*>(payload) + length);
}

class RecordingStream : public Stream {
 public:
  size_t write(uint8_t c) override {
    buffer_.push_back(c);
    return 1;
  }

  size_t write(const uint8_t* data, size_t size) override {
    if (!data) {
      return 0;
    }
    buffer_.insert(buffer_.end(), data, data + size);
    return size;
  }

  void clear() { buffer_.clear(); }

  const std::vector<uint8_t>& data() const { return buffer_; }

 private:
  std::vector<uint8_t> buffer_;
};

// Rebinds the global Bridge instance to a host-side stream while in scope.
class ScopedBridgeBinding {
 public:
  explicit ScopedBridgeBinding(Stream& stream) {
    new (&Bridge) BridgeClass(stream);
    Bridge.begin();
  }

  ~ScopedBridgeBinding() {
    new (&Bridge) BridgeClass(Serial1);
    Bridge.begin();
  }

  ScopedBridgeBinding(const ScopedBridgeBinding&) = delete;
  ScopedBridgeBinding& operator=(const ScopedBridgeBinding&) = delete;
};

std::vector<Frame> decode_frames(const std::vector<uint8_t>& bytes) {
  FrameParser parser;
  Frame frame{};
  std::vector<Frame> frames;
  for (uint8_t byte : bytes) {
    if (parser.consume(byte, frame)) {
      frames.push_back(frame);
    }
  }
  return frames;
}

void test_datastore_get_response_dispatches_handler() {
  RecordingStream stream;
  BridgeClass bridge(stream);

  bridge._pending_datastore_head = 0;
  bridge._pending_datastore_count = 0;
  std::memset(bridge._pending_datastore_key_lengths, 0,
              sizeof(bridge._pending_datastore_key_lengths));
  std::memset(bridge._pending_datastore_keys, 0,
              sizeof(bridge._pending_datastore_keys));

  DatastoreHandlerState handler_state;
  DatastoreHandlerState::instance = &handler_state;
  bridge.onDataStoreGetResponse(datastore_handler_trampoline);

  bool enqueued = bridge._trackPendingDatastoreKey("thermostat");
  assert(enqueued);

  Frame frame{};
  frame.header.version = PROTOCOL_VERSION;
  frame.header.command_id = command_value(CommandId::CMD_DATASTORE_GET_RESP);
  frame.header.payload_length = 1 + 5;
  frame.payload[0] = 5;
  std::memcpy(frame.payload + 1, "23.7C", 5);

  bridge.dispatch(frame);

  assert(handler_state.called);
  assert(handler_state.key == "thermostat");
  assert(handler_state.value == "23.7C");
  DatastoreHandlerState::instance = nullptr;
}

void test_datastore_queue_rejects_overflow() {
  RecordingStream stream;
  BridgeClass bridge(stream);

  bool first = bridge._trackPendingDatastoreKey("alpha");
  assert(first);

  bool second = bridge._trackPendingDatastoreKey("beta");
  assert(!second);

  const char* key = bridge._popPendingDatastoreKey();
  assert(std::strcmp(key, "alpha") == 0);

  const char* empty = bridge._popPendingDatastoreKey();
  assert(empty[0] == '\0');
}

void test_console_write_and_flow_control() {
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  Console.begin();

  const uint8_t payload[] = {0xAA, 0xBB, 0xCC};
  size_t written = Console.write(payload, sizeof(payload));
  assert(written == sizeof(payload));

  auto frames = decode_frames(stream.data());
  assert(frames.size() == 1);
  const Frame& console_frame = frames.front();
  assert(console_frame.header.command_id == command_value(CommandId::CMD_CONSOLE_WRITE));
  assert(console_frame.header.payload_length == sizeof(payload));
  assert(std::memcmp(console_frame.payload, payload, sizeof(payload)) == 0);
  Bridge._handleAck(command_value(CommandId::CMD_CONSOLE_WRITE));
  stream.clear();

  std::vector<uint8_t> large(MAX_PAYLOAD_SIZE + 5, 0x5A);
  size_t large_written = Console.write(large.data(), large.size());
  assert(large_written == large.size());
  auto limited_frames = decode_frames(stream.data());
  assert(limited_frames.size() == 1);
  assert(limited_frames[0].header.command_id == command_value(CommandId::CMD_CONSOLE_WRITE));
  assert(limited_frames[0].header.payload_length == MAX_PAYLOAD_SIZE);
  Bridge._handleAck(command_value(CommandId::CMD_CONSOLE_WRITE));
  stream.clear();

  Bridge.begin();
  Console.begin();

  std::vector<uint8_t> inbound(CONSOLE_BUFFER_HIGH_WATER + 2, 0x34);
  Console._push(inbound.data(), inbound.size());
  assert(Console.available() == static_cast<int>(inbound.size()));
  int peeked = Console.peek();
  assert(peeked == 0x34);

  auto xoff_frames = decode_frames(stream.data());
  assert(!xoff_frames.empty());
  assert(xoff_frames.back().header.command_id == command_value(CommandId::CMD_XOFF));
  Bridge._handleAck(command_value(CommandId::CMD_XOFF));
  stream.clear();

  for (size_t i = 0; i < inbound.size(); ++i) {
    int value = Console.read();
    assert(value == 0x34);
  }
  assert(Console.read() == -1);

  auto xon_frames = decode_frames(stream.data());
  assert(!xon_frames.empty());
  assert(xon_frames.back().header.command_id == command_value(CommandId::CMD_XON));
  Bridge._handleAck(command_value(CommandId::CMD_XON));

  Console.flush();
}

void test_datastore_put_and_request_behavior() {
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  const char* key = "temp";
  const char* value = "23.5";

  DataStore.put(key, value);
  auto put_frames = decode_frames(stream.data());
  assert(put_frames.size() == 1);
  const Frame& put_frame = put_frames.front();
  assert(put_frame.header.command_id == command_value(CommandId::CMD_DATASTORE_PUT));
  uint8_t key_len = static_cast<uint8_t>(std::strlen(key));
  uint8_t value_len = static_cast<uint8_t>(std::strlen(value));
  assert(put_frame.payload[0] == key_len);
  assert(std::memcmp(put_frame.payload + 1, key, key_len) == 0);
  assert(put_frame.payload[1 + key_len] == value_len);
  assert(std::memcmp(put_frame.payload + 2 + key_len, value, value_len) == 0);
  Bridge._handleAck(command_value(CommandId::CMD_DATASTORE_PUT));
  stream.clear();

  DataStore.put(nullptr, value);
  DataStore.put("", value);
  std::string oversized_key(BridgeClass::kMaxDatastoreKeyLength + 1, 'k');
  DataStore.put(oversized_key.c_str(), value);
  std::string oversized_value(BridgeClass::kMaxDatastoreKeyLength + 1, 'v');
  DataStore.put(key, oversized_value.c_str());
  assert(stream.data().empty());

  DataStore.requestGet(key);
  auto get_frames = decode_frames(stream.data());
  assert(get_frames.size() == 1);
  assert(get_frames.front().header.command_id == command_value(CommandId::CMD_DATASTORE_GET));
  Bridge._handleAck(command_value(CommandId::CMD_DATASTORE_GET));
  stream.clear();

  DataStore.requestGet("other");
  auto status_frames = decode_frames(stream.data());
  assert(!status_frames.empty());
  const Frame& status_frame = status_frames.back();
  assert(status_frame.header.command_id == status_value(StatusCode::STATUS_ERROR));
  std::string status_message(
      reinterpret_cast<const char*>(status_frame.payload),
      reinterpret_cast<const char*>(status_frame.payload) +
          status_frame.header.payload_length);
  assert(status_message == "datastore_queue_full");
  (void)Bridge._popPendingDatastoreKey();
}

void test_mailbox_send_and_requests_emit_commands() {
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  const char* msg = "hello";
  Mailbox.send(msg);
  auto frames = decode_frames(stream.data());
  assert(frames.size() == 1);
  const Frame& mailbox_frame = frames.front();
  assert(mailbox_frame.header.command_id == command_value(CommandId::CMD_MAILBOX_PUSH));
  size_t msg_len = std::strlen(msg);
  assert(mailbox_frame.header.payload_length == msg_len + 2);
  uint16_t encoded_len = read_u16_be(mailbox_frame.payload);
  assert(encoded_len == msg_len);
  assert(std::memcmp(mailbox_frame.payload + 2, msg, msg_len) == 0);
  Bridge._handleAck(command_value(CommandId::CMD_MAILBOX_PUSH));
  stream.clear();

  std::vector<uint8_t> raw(MAX_PAYLOAD_SIZE, 0x41);
  Mailbox.send(raw.data(), raw.size());
  auto raw_frames = decode_frames(stream.data());
  assert(raw_frames.size() == 1);
  size_t capped_len = MAX_PAYLOAD_SIZE - 2;
  assert(raw_frames[0].header.payload_length == capped_len + 2);
  uint16_t encoded_raw_len = read_u16_be(raw_frames[0].payload);
  assert(encoded_raw_len == capped_len);
  Bridge._handleAck(command_value(CommandId::CMD_MAILBOX_PUSH));
  stream.clear();

  Mailbox.requestRead();
  auto read_frames = decode_frames(stream.data());
  assert(read_frames.size() == 1);
  assert(read_frames[0].header.command_id == command_value(CommandId::CMD_MAILBOX_READ));
  assert(read_frames[0].header.payload_length == 0);
  Bridge._handleAck(command_value(CommandId::CMD_MAILBOX_READ));
  stream.clear();

  Mailbox.requestAvailable();
  auto avail_frames = decode_frames(stream.data());
  assert(avail_frames.size() == 1);
  assert(avail_frames[0].header.command_id == command_value(CommandId::CMD_MAILBOX_AVAILABLE));
  assert(avail_frames[0].header.payload_length == 0);
  Bridge._handleAck(command_value(CommandId::CMD_MAILBOX_AVAILABLE));
  stream.clear();
}

void test_filesystem_write_and_remove_payloads() {
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  const char* path = "/tmp/data";
  std::vector<uint8_t> blob(MAX_PAYLOAD_SIZE, 0xEE);
  FileSystem.write(path, blob.data(), blob.size());
  auto write_frames = decode_frames(stream.data());
  assert(write_frames.size() == 1);
  const Frame& write_frame = write_frames.front();
  assert(write_frame.header.command_id == command_value(CommandId::CMD_FILE_WRITE));
  uint8_t path_len = static_cast<uint8_t>(std::strlen(path));
  assert(write_frame.payload[0] == path_len);
  assert(std::memcmp(write_frame.payload + 1, path, path_len) == 0);
  uint16_t encoded_len = read_u16_be(write_frame.payload + 1 + path_len);
  size_t max_data = MAX_PAYLOAD_SIZE - 3 - path_len;
  assert(encoded_len == max_data);
  assert(write_frame.header.payload_length == path_len + encoded_len + 3);
  Bridge._handleAck(command_value(CommandId::CMD_FILE_WRITE));
  stream.clear();

  FileSystem.write(nullptr, blob.data(), blob.size());
  FileSystem.write(path, nullptr, blob.size());
  FileSystem.write("", blob.data(), blob.size());
  std::string long_path(300, 'a');
  FileSystem.write(long_path.c_str(), blob.data(), blob.size());
  assert(stream.data().empty());

  FileSystem.remove(path);
  auto remove_frames = decode_frames(stream.data());
  assert(remove_frames.size() == 1);
  const Frame& remove_frame = remove_frames.front();
  assert(remove_frame.header.command_id == command_value(CommandId::CMD_FILE_REMOVE));
  assert(remove_frame.payload[0] == path_len);
  assert(std::memcmp(remove_frame.payload + 1, path, path_len) == 0);
  Bridge._handleAck(command_value(CommandId::CMD_FILE_REMOVE));
  stream.clear();

  FileSystem.remove("");
  assert(stream.data().empty());
}

void test_process_kill_encodes_pid() {
  RecordingStream stream;
  ScopedBridgeBinding binding(stream);

  Process.kill(0x1234);
  auto frames = decode_frames(stream.data());
  assert(frames.size() == 1);
  const Frame& frame = frames.front();
  assert(frame.header.command_id == command_value(CommandId::CMD_PROCESS_KILL));
  assert(frame.header.payload_length == 2);
  uint16_t encoded = read_u16_be(frame.payload);
  assert(encoded == 0x1234);
  Bridge._handleAck(command_value(CommandId::CMD_PROCESS_KILL));
  stream.clear();
}

void test_mailbox_read_response_delivers_payload() {
  RecordingStream stream;
  BridgeClass bridge(stream);

  MailboxHandlerState mailbox_state;
  MailboxHandlerState::instance = &mailbox_state;
  bridge.onMailboxMessage(mailbox_handler_trampoline);

  const char* payload = "hello-linux";
  const uint16_t payload_len = static_cast<uint16_t>(std::strlen(payload));

  Frame frame{};
  frame.header.version = PROTOCOL_VERSION;
  frame.header.command_id = command_value(CommandId::CMD_MAILBOX_READ_RESP);
  frame.header.payload_length = static_cast<uint16_t>(2 + payload_len);
  write_u16_be(frame.payload, payload_len);
  std::memcpy(frame.payload + 2, payload, payload_len);

  bridge.dispatch(frame);

  assert(mailbox_state.called);
  assert(mailbox_state.message == payload);
  MailboxHandlerState::instance = nullptr;
}

void test_process_poll_response_requeues_on_streaming_output() {
  RecordingStream stream;
  BridgeClass bridge(stream);

  bridge._pending_process_poll_head = 0;
  bridge._pending_process_poll_count = 0;
  std::memset(bridge._pending_process_pids, 0,
              sizeof(bridge._pending_process_pids));

  const uint16_t pid = 0x1234;
  bool enqueued = bridge._pushPendingProcessPid(pid);
  assert(enqueued);

  ProcessPollHandlerState poll_state;
  ProcessPollHandlerState::instance = &poll_state;
  bridge.onProcessPollResponse(process_poll_handler_trampoline);

  constexpr uint8_t stdout_text[] = {'o', 'k'};

  Frame frame{};
  frame.header.version = PROTOCOL_VERSION;
  frame.header.command_id = command_value(CommandId::CMD_PROCESS_POLL_RESP);
  frame.header.payload_length = 6 + sizeof(stdout_text);
  uint8_t* cursor = frame.payload;
  *cursor++ = status_value(StatusCode::STATUS_OK);
  *cursor++ = 0x7F;
  write_u16_be(cursor, sizeof(stdout_text));
  cursor += 2;
  write_u16_be(cursor, 0);
  cursor += 2;
  std::memcpy(cursor, stdout_text, sizeof(stdout_text));

  stream.clear();
  bridge.dispatch(frame);

  assert(poll_state.called);
  assert(poll_state.status == StatusCode::STATUS_OK);
  assert(poll_state.exit_code == 0x7F);
  assert(poll_state.stdout_text == "ok");
  assert(poll_state.stderr_text.empty());
  ProcessPollHandlerState::instance = nullptr;

  const auto frames = decode_frames(stream.data());
  assert(!frames.empty());
  const Frame& resend = frames.back();
  assert(resend.header.command_id == command_value(CommandId::CMD_PROCESS_POLL));
  assert(resend.header.payload_length == 2);
  uint16_t encoded_pid = read_u16_be(resend.payload);
  assert(encoded_pid == pid);
}

void test_begin_preserves_binary_shared_secret_length() {
  RecordingStream stream_explicit;
  BridgeClass bridge_explicit(stream_explicit);

  const uint8_t secret_bytes[] = {0x00, 0x01, 0x02, 0x00, 0x03};
  const char* binary_secret = reinterpret_cast<const char*>(secret_bytes);
  bridge_explicit.begin(115200, binary_secret, sizeof(secret_bytes));

  assert(
      reinterpret_cast<const void*>(bridge_explicit._shared_secret) ==
      reinterpret_cast<const void*>(binary_secret));
  assert(bridge_explicit._shared_secret_len == sizeof(secret_bytes));

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
  assert(explicit_has_entropy);

  RecordingStream stream_default;
  BridgeClass bridge_default(stream_default);
  bridge_default.begin(115200, binary_secret);

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
  assert(truncated_all_zero);
  assert(std::memcmp(explicit_tag, truncated_tag, sizeof(explicit_tag)) != 0);
}

void test_ack_flushes_pending_queue_after_response() {
  RecordingStream stream;
  BridgeClass bridge(stream);

  bridge.begin();

  const uint8_t first_payload[] = {0x42};
  bool sent = bridge.sendFrame(
      CommandId::CMD_CONSOLE_WRITE,
      BufferView(first_payload, sizeof(first_payload)));
  assert(sent);
  assert(bridge._awaiting_ack);

  const uint8_t queued_payload[] = {0xAA, 0xBB};
    bool enqueued = bridge._enqueuePendingTx(
      command_value(CommandId::CMD_MAILBOX_PUSH),
      BufferView(queued_payload, sizeof(queued_payload)));
  assert(enqueued);
  assert(bridge._pending_tx_count == 1);

  auto before = decode_frames(stream.data());
  size_t before_count = before.size();

  bridge._handleAck(command_value(CommandId::CMD_CONSOLE_WRITE));

  auto after = decode_frames(stream.data());
  assert(after.size() == before_count + 1);
  const Frame& flushed = after.back();
  assert(flushed.header.command_id == command_value(CommandId::CMD_MAILBOX_PUSH));
  assert(flushed.header.payload_length == sizeof(queued_payload));
  assert(std::memcmp(flushed.payload, queued_payload, sizeof(queued_payload)) == 0);
  assert(bridge._pending_tx_count == 0);
  assert(bridge._awaiting_ack);
}

void test_status_ack_frame_clears_pending_state_via_dispatch() {
  RecordingStream stream;
  BridgeClass bridge(stream);

  bridge.begin();
  StatusHandlerState status_state;
  StatusHandlerState::instance = &status_state;
  bridge.onStatus(status_handler_trampoline);

  const uint8_t payload[] = {0x55};
  bool sent = bridge.sendFrame(
      CommandId::CMD_CONSOLE_WRITE, BufferView(payload, sizeof(payload)));
  assert(sent);
  assert(bridge._awaiting_ack);

  Frame ack{};
  ack.header.version = PROTOCOL_VERSION;
  ack.header.command_id = status_value(StatusCode::STATUS_ACK);
  ack.header.payload_length = 2;
  write_u16_be(ack.payload, command_value(CommandId::CMD_CONSOLE_WRITE));

  bridge.dispatch(ack);

  assert(!bridge._awaiting_ack);
  assert(status_state.called);
  assert(status_state.status_code == StatusCode::STATUS_ACK);
  StatusHandlerState::instance = nullptr;
}

void test_status_error_frame_dispatches_handler() {
  RecordingStream stream;
  BridgeClass bridge(stream);

  StatusHandlerState status_state;
  StatusHandlerState::instance = &status_state;
  bridge.onStatus(status_handler_trampoline);

  const char* message = "remote_fault";
  Frame frame{};
  frame.header.version = PROTOCOL_VERSION;
  frame.header.command_id = status_value(StatusCode::STATUS_ERROR);
  frame.header.payload_length = static_cast<uint16_t>(std::strlen(message));
  std::memcpy(frame.payload, message, frame.header.payload_length);

  bridge.dispatch(frame);

  assert(status_state.called);
  assert(status_state.status_code == StatusCode::STATUS_ERROR);
  assert(status_state.payload == message);
  StatusHandlerState::instance = nullptr;
}

void test_malformed_status_triggers_retransmit() {
  RecordingStream stream;
  BridgeClass bridge(stream);

  const uint8_t payload[] = {0x10, 0x20, 0x30};
  bool sent = bridge.sendFrame(
      CommandId::CMD_MAILBOX_PUSH, BufferView(payload, sizeof(payload)));
  assert(sent);
  assert(bridge._awaiting_ack);

  auto before = decode_frames(stream.data());
  assert(before.size() == 1);

  bridge._handleMalformed(command_value(CommandId::CMD_MAILBOX_PUSH));

  auto after = decode_frames(stream.data());
  assert(after.size() == 2);
  const Frame& resent = after.back();
  assert(resent.header.command_id == command_value(CommandId::CMD_MAILBOX_PUSH));
  assert(resent.header.payload_length == sizeof(payload));
  assert(std::memcmp(resent.payload, payload, sizeof(payload)) == 0);
  assert(bridge._retry_count == 1);
}

void test_link_sync_generates_tag_and_ack() {
  RecordingStream stream;
  BridgeClass bridge(stream);

  const char* secret = "unit-test-secret";
  bridge.begin(115200, secret);

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
  bridge.dispatch(frame);

  auto frames = decode_frames(stream.data());
  assert(frames.size() == 2);
  const Frame& sync = frames.front();
  assert(sync.header.command_id == command_value(CommandId::CMD_LINK_SYNC_RESP));
  assert(sync.header.payload_length == sizeof(nonce) + 16);
  assert(std::memcmp(sync.payload, nonce, sizeof(nonce)) == 0);
  uint8_t expected_tag[16];
  bridge._computeHandshakeTag(nonce, sizeof(nonce), expected_tag);
  assert(std::memcmp(sync.payload + sizeof(nonce), expected_tag, 16) == 0);

  const Frame& ack = frames.back();
  assert(ack.header.command_id == status_value(StatusCode::STATUS_ACK));
  assert(ack.header.payload_length == 2);
  assert(read_u16_be(ack.payload) == command_value(CommandId::CMD_LINK_SYNC));
}

void test_link_sync_without_secret_replays_nonce_only() {
  RecordingStream stream;
  BridgeClass bridge(stream);

  bridge.begin(115200, nullptr);

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
  bridge.dispatch(frame);

  auto frames = decode_frames(stream.data());
  assert(frames.size() == 2);
  const Frame& sync = frames.front();
  assert(sync.header.command_id == command_value(CommandId::CMD_LINK_SYNC_RESP));
  assert(sync.header.payload_length == sizeof(nonce));
  assert(std::memcmp(sync.payload, nonce, sizeof(nonce)) == 0);
}

void test_ack_timeout_emits_status_and_resets_state() {
  RecordingStream stream;
  BridgeClass bridge(stream);

  StatusHandlerState status_state;
  StatusHandlerState::instance = &status_state;
  bridge.onStatus(status_handler_trampoline);

  const uint8_t payload[] = {0x99};
  bool sent = bridge.sendFrame(
      CommandId::CMD_MAILBOX_PUSH, BufferView(payload, sizeof(payload)));
  assert(sent);
  assert(bridge._awaiting_ack);

  bridge._retry_count = BridgeClass::kMaxAckRetries;
  bridge._last_send_millis = 1000;
  bridge._processAckTimeout();

  assert(status_state.called);
  assert(status_state.status_code == StatusCode::STATUS_TIMEOUT);
  assert(!bridge._awaiting_ack);
  StatusHandlerState::instance = nullptr;
}

void test_process_run_rejects_oversized_payload() {
  RecordingStream stream;
  BridgeClass bridge(stream);

  StatusHandlerState status_state;
  StatusHandlerState::instance = &status_state;
  bridge.onStatus(status_handler_trampoline);

  std::string huge(rpc::MAX_PAYLOAD_SIZE + 4, 'x');
  bridge.requestProcessRun(huge.c_str());

  auto frames = decode_frames(stream.data());
  assert(frames.size() == 1);
  const Frame& status_frame = frames.front();
  assert(status_frame.header.command_id == status_value(StatusCode::STATUS_ERROR));
  std::string message(
      reinterpret_cast<const char*>(status_frame.payload),
      reinterpret_cast<const char*>(status_frame.payload) +
          status_frame.header.payload_length);
  assert(message == "process_run_payload_too_large");
  assert(status_state.called);
  assert(status_state.payload == "process_run_payload_too_large");

  bridge._handleAck(status_value(StatusCode::STATUS_ERROR));
  StatusHandlerState::instance = nullptr;
}

void test_apply_timing_config_accepts_valid_payload() {
  RecordingStream stream;
  BridgeClass bridge(stream);

  uint8_t payload[RPC_HANDSHAKE_CONFIG_SIZE] = {};
  const uint16_t ack_timeout = RPC_HANDSHAKE_ACK_TIMEOUT_MIN_MS + 5;
  const uint8_t retry_limit = RPC_HANDSHAKE_RETRY_LIMIT_MIN + 1;
  const uint32_t response_timeout =
      RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS - 250;

  rpc::write_u16_be(payload, ack_timeout);
  payload[2] = retry_limit;
  rpc::write_u32_be(payload + 3, response_timeout);

  bridge._applyTimingConfig(payload, sizeof(payload));

  assert(bridge._ack_timeout_ms == ack_timeout);
  assert(bridge._ack_retry_limit == retry_limit);
  assert(bridge._response_timeout_ms == response_timeout);
}

void test_apply_timing_config_rejects_invalid_payload() {
  RecordingStream stream;
  BridgeClass bridge(stream);

  uint8_t payload[RPC_HANDSHAKE_CONFIG_SIZE] = {};
  const uint16_t invalid_ack_timeout = RPC_HANDSHAKE_ACK_TIMEOUT_MAX_MS + 5;
  const uint8_t invalid_retry_limit = RPC_HANDSHAKE_RETRY_LIMIT_MAX + 1;
  const uint32_t invalid_response_timeout =
      RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS + 1;

  rpc::write_u16_be(payload, invalid_ack_timeout);
  payload[2] = invalid_retry_limit;
  rpc::write_u32_be(payload + 3, invalid_response_timeout);

  bridge._applyTimingConfig(payload, sizeof(payload));

  assert(bridge._ack_timeout_ms == BridgeClass::kAckTimeoutMs);
  assert(bridge._ack_retry_limit == BridgeClass::kMaxAckRetries);
  assert(bridge._response_timeout_ms == RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS);

  bridge._ack_timeout_ms = 1;
  bridge._ack_retry_limit = 1;
  bridge._response_timeout_ms = RPC_HANDSHAKE_RESPONSE_TIMEOUT_MAX_MS;
  bridge._applyTimingConfig(payload, RPC_HANDSHAKE_CONFIG_SIZE - 1);

  assert(bridge._ack_timeout_ms == BridgeClass::kAckTimeoutMs);
  assert(bridge._ack_retry_limit == BridgeClass::kMaxAckRetries);
  assert(bridge._response_timeout_ms == RPC_HANDSHAKE_RESPONSE_TIMEOUT_MIN_MS);
}

}  // namespace

int main() {
  test_datastore_get_response_dispatches_handler();
  test_datastore_queue_rejects_overflow();
  test_console_write_and_flow_control();
  test_datastore_put_and_request_behavior();
  test_mailbox_send_and_requests_emit_commands();
  test_filesystem_write_and_remove_payloads();
  test_process_kill_encodes_pid();
  test_mailbox_read_response_delivers_payload();
  test_process_poll_response_requeues_on_streaming_output();
  test_begin_preserves_binary_shared_secret_length();
  test_ack_flushes_pending_queue_after_response();
  test_status_ack_frame_clears_pending_state_via_dispatch();
  test_status_error_frame_dispatches_handler();
  test_malformed_status_triggers_retransmit();
  test_link_sync_generates_tag_and_ack();
  test_link_sync_without_secret_replays_nonce_only();
  test_ack_timeout_emits_status_and_resets_state();
  test_process_run_rejects_oversized_payload();
  test_apply_timing_config_accepts_valid_payload();
  test_apply_timing_config_rejects_invalid_payload();
  return 0;
}
