#include "services/Mailbox.h"

#include <etl/algorithm.h>

#if BRIDGE_ENABLE_MAILBOX

#include "Bridge.h"

MailboxClass::MailboxClass() {}

pw::rpc::NanopbUnaryReceiver<rpc_pb_MailboxReadResponse>
    MailboxClass::_read_call;
pw::rpc::NanopbUnaryReceiver<rpc_pb_MailboxAvailableResponse>
    MailboxClass::_avail_call;
typename MailboxClass::MessageCallback MailboxClass::_message_callback;
typename MailboxClass::AvailableCallback MailboxClass::_available_callback;
etl::circular_buffer<MailboxClass::MailboxBuffer, MAILBOX_QUEUE_CAPACITY>
    MailboxClass::_queue;

void MailboxClass::push(etl::span<const uint8_t> data) {
  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }
  rpc_pb_MailboxPush p = rpc_pb_MailboxPush_init_default;
  bridge::pb::copy_bytes(p.data, data);
  (void)Bridge.mailboxClient().Push(p);
}

bool MailboxClass::requestRead() {
  if (!Bridge.isRpcTxReady()) return false;
  rpc_pb_SubscribeRequest req = rpc_pb_SubscribeRequest_init_default;
  _read_call = Bridge.mailboxClient().Read(
      req, [](const rpc_pb_MailboxReadResponse& resp, pw::Status status) {
        static_cast<void>(status);
        Mailbox._enqueue(
            etl::span<const uint8_t>(resp.content.bytes, resp.content.size));
      });
  return true;
}

bool MailboxClass::requestAvailable() {
  if (!Bridge.isRpcTxReady()) return false;
  rpc_pb_SubscribeRequest req = rpc_pb_SubscribeRequest_init_default;
  _avail_call = Bridge.mailboxClient().Available(
      req, [](const rpc_pb_MailboxAvailableResponse& resp, pw::Status status) {
        static_cast<void>(status);
        Mailbox._onAvailableResponse(resp);
      });
  return true;
}

void MailboxClass::signalProcessed(uint32_t message_id) {
  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }
  rpc_pb_MailboxProcessed p = rpc_pb_MailboxProcessed_init_default;
  p.message_id = message_id;
  (void)Bridge.mailboxClient().Processed(p);
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

void MailboxClass::onLost() {
  _read_call = {};
  _avail_call = {};
  _queue.clear();
}

MailboxType Mailbox;

#endif
