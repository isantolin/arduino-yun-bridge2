#include <cassert>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

#define private public
#define protected public
#include "Bridge.h"
#undef private
#undef protected

#include "protocol/rpc_protocol.h"

using namespace rpc;

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
  uint8_t status = 0xFF;
  uint8_t exit_code = 0xFF;
  std::string stdout_text;
  std::string stderr_text;
};

ProcessPollHandlerState* ProcessPollHandlerState::instance = nullptr;

void process_poll_handler_trampoline(
    uint8_t status,
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

  bridge._trackPendingDatastoreKey("thermostat");

  Frame frame{};
  frame.header.version = PROTOCOL_VERSION;
  frame.header.command_id = CMD_DATASTORE_GET_RESP;
  frame.header.payload_length = 1 + 5;
  frame.payload[0] = 5;
  std::memcpy(frame.payload + 1, "23.7C", 5);

  bridge.dispatch(frame);

  assert(handler_state.called);
  assert(handler_state.key == "thermostat");
  assert(handler_state.value == "23.7C");
  DatastoreHandlerState::instance = nullptr;
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
  frame.header.command_id = CMD_MAILBOX_READ_RESP;
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
  frame.header.command_id = CMD_PROCESS_POLL_RESP;
  frame.header.payload_length = 6 + sizeof(stdout_text);
  uint8_t* cursor = frame.payload;
  *cursor++ = STATUS_OK;
  *cursor++ = 0x7F;
  write_u16_be(cursor, sizeof(stdout_text));
  cursor += 2;
  write_u16_be(cursor, 0);
  cursor += 2;
  std::memcpy(cursor, stdout_text, sizeof(stdout_text));

  stream.clear();
  bridge.dispatch(frame);

  assert(poll_state.called);
  assert(poll_state.status == STATUS_OK);
  assert(poll_state.exit_code == 0x7F);
  assert(poll_state.stdout_text == "ok");
  assert(poll_state.stderr_text.empty());
  ProcessPollHandlerState::instance = nullptr;

  const auto frames = decode_frames(stream.data());
  assert(!frames.empty());
  const Frame& resend = frames.back();
  assert(resend.header.command_id == CMD_PROCESS_POLL);
  assert(resend.header.payload_length == 2);
  uint16_t encoded_pid = read_u16_be(resend.payload);
  assert(encoded_pid == pid);
}

void test_ack_flushes_pending_queue_after_response() {
  RecordingStream stream;
  BridgeClass bridge(stream);

  bridge.begin();

  const uint8_t first_payload[] = {0x42};
  bool sent = bridge.sendFrame(
      CMD_CONSOLE_WRITE, first_payload, sizeof(first_payload));
  assert(sent);
  assert(bridge._awaiting_ack);

  const uint8_t queued_payload[] = {0xAA, 0xBB};
  bool enqueued = bridge._enqueuePendingTx(
      CMD_MAILBOX_PUSH, queued_payload, sizeof(queued_payload));
  assert(enqueued);
  assert(bridge._pending_tx_count == 1);

  auto before = decode_frames(stream.data());
  size_t before_count = before.size();

  bridge._handleAck(CMD_CONSOLE_WRITE);

  auto after = decode_frames(stream.data());
  assert(after.size() == before_count + 1);
  const Frame& flushed = after.back();
  assert(flushed.header.command_id == CMD_MAILBOX_PUSH);
  assert(flushed.header.payload_length == sizeof(queued_payload));
  assert(std::memcmp(flushed.payload, queued_payload, sizeof(queued_payload)) == 0);
  assert(bridge._pending_tx_count == 0);
  assert(bridge._awaiting_ack);
}

void test_malformed_status_triggers_retransmit() {
  RecordingStream stream;
  BridgeClass bridge(stream);

  const uint8_t payload[] = {0x10, 0x20, 0x30};
  bool sent = bridge.sendFrame(CMD_MAILBOX_PUSH, payload, sizeof(payload));
  assert(sent);
  assert(bridge._awaiting_ack);

  auto before = decode_frames(stream.data());
  assert(before.size() == 1);

  bridge._handleMalformed(CMD_MAILBOX_PUSH);

  auto after = decode_frames(stream.data());
  assert(after.size() == 2);
  const Frame& resent = after.back();
  assert(resent.header.command_id == CMD_MAILBOX_PUSH);
  assert(resent.header.payload_length == sizeof(payload));
  assert(std::memcmp(resent.payload, payload, sizeof(payload)) == 0);
  assert(bridge._retry_count == 1);
}

}  // namespace

int main() {
  test_datastore_get_response_dispatches_handler();
  test_mailbox_read_response_delivers_payload();
  test_process_poll_response_requeues_on_streaming_output();
  test_ack_flushes_pending_queue_after_response();
  test_malformed_status_triggers_retransmit();
  return 0;
}
