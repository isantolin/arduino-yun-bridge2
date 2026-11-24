#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif

#include "services/Mailbox.h"

#include <etl/algorithm.h>
#include <pw_span/span.h>

#if BRIDGE_ENABLE_MAILBOX

#include "Bridge.h"

MailboxClass::MailboxClass() {}

pw::rpc::NanopbUnaryReceiver<rpc_pb_Empty> MailboxClass::_push_call;
pw::rpc::NanopbUnaryReceiver<rpc_pb_MailboxReadResponse>
    MailboxClass::_read_call;
pw::rpc::NanopbUnaryReceiver<rpc_pb_MailboxAvailableResponse>
    MailboxClass::_avail_call;
typename MailboxClass::MessageCallback MailboxClass::_message_callback;
typename MailboxClass::AvailableCallback MailboxClass::_available_callback;
pw::InlineQueue<MailboxClass::MailboxBuffer, MAILBOX_QUEUE_CAPACITY>
    MailboxClass::_queue;

void MailboxClass::push(pw::span<const uint8_t> data) {
  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(pw::Status::Unavailable());
    return;
  }
  rpc_pb_MailboxPush p = rpc_pb_MailboxPush_init_default;
  bridge::pb::copy_bytes(p.data, data);
  _push_call = Bridge.mailboxClient().Push(
      p,
      [](const rpc_pb_Empty&, pw::Status status) {
        if (!status.ok()) {
          Bridge.emitStatus(status);
        }
      },
      [](pw::Status status) { Bridge.emitStatus(status); });
}

bool MailboxClass::requestRead() {
  if (_read_call.active()) {
    Bridge.emitStatus(pw::Status::ResourceExhausted());
    return false;
  }
  if (!Bridge.isRpcTxReady()) return false;
  rpc_pb_SubscribeRequest req = rpc_pb_SubscribeRequest_init_default;
  _read_call = Bridge.mailboxClient().Read(
      req,
      [](const rpc_pb_MailboxReadResponse& resp, pw::Status status) {
        Mailbox._onReadResponse(resp, status);
      },
      [](pw::Status status) { Bridge.emitStatus(status); });
  return _read_call.active();
}

bool MailboxClass::requestAvailable() {
  if (_avail_call.active()) {
    Bridge.emitStatus(pw::Status::ResourceExhausted());
    return false;
  }
  if (!Bridge.isRpcTxReady()) return false;
  rpc_pb_SubscribeRequest req = rpc_pb_SubscribeRequest_init_default;
  _avail_call = Bridge.mailboxClient().Available(
      req,
      [](const rpc_pb_MailboxAvailableResponse& resp, pw::Status status) {
        Mailbox._onAvailableResponse(resp, status);
      },
      [](pw::Status status) { Bridge.emitStatus(status); });
  return _avail_call.active();
}

void MailboxClass::signalProcessed(uint32_t message_id) {
  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(pw::Status::Unavailable());
    return;
  }
  rpc_pb_MailboxProcessed p = rpc_pb_MailboxProcessed_init_default;
  p.message_id = message_id;
  (void)Bridge.mailboxClient().Processed(p);
}

void MailboxClass::_enqueue(pw::span<const uint8_t> data) {
  if (data.empty()) return;
  if (!_queue.full()) {
    _queue.emplace();
    MailboxBuffer& m = _queue.back();
    const size_t sz = etl::min(data.size(), m.max_size());
    m.assign(data.data(), data.data() + sz);
  }
}

void MailboxClass::_onReadResponse(const rpc_pb_MailboxReadResponse& msg,
                                   pw::Status status) {
  if (!status.ok()) {
    Bridge.emitStatus(status);
    return;
  }
  _enqueue(pw::span<const uint8_t>(msg.content.bytes, msg.content.size));
}

void MailboxClass::_onAvailableResponse(
    const rpc_pb_MailboxAvailableResponse& msg, pw::Status status) {
  if (!status.ok()) {
    Bridge.emitStatus(status);
    return;
  }
  if (_available_callback) {
    _available_callback(msg.count);
  }
}

void MailboxClass::process() {
  if (!_queue.empty() && _message_callback) {
    const auto& m = _queue.front();
    _message_callback(pw::span<const uint8_t>(m.data(), m.size()));
    _queue.pop();
  }
}

void MailboxClass::onLost() {
  _push_call = {};
  _read_call = {};
  _avail_call = {};
  _queue.clear();
}

MailboxType Mailbox;

#endif
