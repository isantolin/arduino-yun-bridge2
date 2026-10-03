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
#include "protocol/rpc_structs.h"

#if !defined(MAILBOX_QUEUE_CAPACITY) || (MAILBOX_QUEUE_CAPACITY == 0)
#undef MAILBOX_QUEUE_CAPACITY
#define MAILBOX_QUEUE_CAPACITY 4
#endif

class MailboxClass : public bridge::BridgeObserver {
 public:
  using MessageCallback = etl::delegate<void(etl::span<const uint8_t>)>;
  using AvailableCallback = etl::delegate<void(uint32_t)>;
  using MailboxBuffer = etl::vector<uint8_t, 64>;

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

  template <typename MsgType, auto FieldPtr>
  static void _onEnqueuePayload(const MsgType& msg) {
    const auto& field = msg.*FieldPtr;
    _enqueue(etl::span<const uint8_t>(field.bytes, field.size));
  }

  static void _onAvailableResponse(
      const rpc::payload::MailboxAvailableResponse& msg);

  static void process();
  void onLost() override;

 // Reemplazar en Mailbox.h (sección private):
 private:
  static void _enqueue(etl::span<const uint8_t> data);
  static MessageCallback _message_callback;
  static AvailableCallback _available_callback;
  static etl::circular_buffer<MailboxBuffer, MAILBOX_QUEUE_CAPACITY> _queue;
};

using MailboxType = MailboxClass;
extern MailboxType Mailbox;

#endif
