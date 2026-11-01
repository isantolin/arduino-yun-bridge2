#include "services/Mailbox.h"

#include <etl/algorithm.h>

#if BRIDGE_ENABLE_MAILBOX

#include "Bridge.h"

MailboxClass::MailboxClass() {}

typename MailboxClass::MessageCallback MailboxClass::_message_callback;
typename MailboxClass::AvailableCallback MailboxClass::_available_callback;
etl::circular_buffer<MailboxClass::MailboxBuffer, MAILBOX_QUEUE_CAPACITY>
    MailboxClass::_queue;

void MailboxClass::push(etl::span<const uint8_t> data) {
  rpc_pb_MailboxPush p = rpc_pb_MailboxPush_init_default;
  bridge::pb::copy_bytes(p.data, data);
  constexpr uint32_t kServiceId = pw::tokenizer::Hash("bridge.MailboxService");
  constexpr uint32_t kMethodId = pw::tokenizer::Hash("Push");
  if (!Bridge.sendRpcRequest(kServiceId, kMethodId, rpc_pb_MailboxPush_fields, p)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
  }
}

bool MailboxClass::requestRead() {
  constexpr uint32_t kServiceId = pw::tokenizer::Hash("bridge.MailboxService");
  constexpr uint32_t kMethodId = pw::tokenizer::Hash("Read");
  return Bridge.sendRpcRequestNoPayload(kServiceId, kMethodId);
}

bool MailboxClass::requestAvailable() {
  constexpr uint32_t kServiceId = pw::tokenizer::Hash("bridge.MailboxService");
  constexpr uint32_t kMethodId = pw::tokenizer::Hash("Available");
  return Bridge.sendRpcRequestNoPayload(kServiceId, kMethodId);
}

void MailboxClass::signalProcessed(uint32_t message_id) {
  rpc_pb_MailboxProcessed p = rpc_pb_MailboxProcessed_init_default;
  p.message_id = message_id;
  constexpr uint32_t kServiceId = pw::tokenizer::Hash("bridge.MailboxService");
  constexpr uint32_t kMethodId = pw::tokenizer::Hash("Processed");
  if (!Bridge.sendRpcRequest(kServiceId, kMethodId, rpc_pb_MailboxProcessed_fields, p)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
  }
}

void MailboxClass::_enqueue(etl::span<const uint8_t> data) {
  if (!_queue.full()) {
    MailboxBuffer m;
    const size_t sz = etl::min(data.size(), m.capacity());
    m.assign(data.data(), data.data() + sz);
    _queue.push(m);
  }
}

void MailboxClass::_onAvailableResponse(
    const rpc_pb_MailboxAvailableResponse& msg) {
  if (_available_callback) {
    _available_callback(msg.count);
  }
}

void MailboxClass::process() {
  if (!_queue.empty() && _message_callback) {
    const auto& m = _queue.front();
    _message_callback(etl::span<const uint8_t>(m.data(), m.size()));
    _queue.pop();
  }
}

void MailboxClass::onLost() { _queue.clear(); }

MailboxType Mailbox;

#endif
