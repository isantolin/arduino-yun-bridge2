#ifndef SERVICES_MAILBOX_H
#define SERVICES_MAILBOX_H

#undef min
#undef max
#include <etl/circular_buffer.h>
#include <etl/delegate.h>
#include <etl/span.h>
#include <etl/vector.h>

#include "Bridge.h"
#include "config/bridge_config.h"
#include "protocol/mcu_wire.pb.h"

#ifndef MAILBOX_QUEUE_CAPACITY
#define MAILBOX_QUEUE_CAPACITY bridge::config::MAX_MAILBOX_QUEUE_SIZE
#endif

class MailboxClass : public bridge::BridgeObserver {
 public:
  using MessageCallback = etl::delegate<void(etl::span<const uint8_t>)>;
  using AvailableCallback = etl::delegate<void(uint32_t)>;
  using MailboxBuffer = etl::vector<uint8_t, rpc::MAX_PAYLOAD_SIZE>;

  MailboxClass();
  static void push(etl::span<const uint8_t> data);
  static bool requestRead();
  static bool requestAvailable();
  static void signalProcessed(uint32_t message_id);

  static void registerMessageCallback(MessageCallback cb) {
    _message_callback = cb;
  }
  static void registerAvailableCallback(AvailableCallback cb) {
    _available_callback = cb;
  }

  static void _onAvailableResponse(const rpc_pb_MailboxAvailableResponse& msg);

  static void _enqueue(etl::span<const uint8_t> data);

  static void process();
  void onLost() override;

 private:
  static pw::rpc::NanopbUnaryReceiver<rpc_pb_MailboxReadResponse> _read_call;
  static pw::rpc::NanopbUnaryReceiver<rpc_pb_MailboxAvailableResponse> _avail_call;
  static MessageCallback _message_callback;
  static AvailableCallback _available_callback;
  static etl::circular_buffer<MailboxBuffer, MAILBOX_QUEUE_CAPACITY> _queue;
};

using MailboxType = MailboxClass;
extern MailboxType Mailbox;

#endif
