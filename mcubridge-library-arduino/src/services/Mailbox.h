#ifndef SERVICES_MAILBOX_H
#define SERVICES_MAILBOX_H

#include "config/bridge_config.h"
#undef min
#undef max
#include <etl/circular_buffer.h>
#include <etl/delegate.h>
#include <etl/span.h>


#include "protocol/rpc_structs.h"



template <typename T = void>
class MailboxClass {
 public:
  using MessageCallback = etl::delegate<void(etl::span<const uint8_t>)>;
  using AvailableCallback = etl::delegate<void(uint32_t)>;

  MailboxClass();
  static void push(etl::span<const uint8_t> data);
  static void requestRead();
  static void requestAvailable();
  static void signalProcessed(uint32_t message_id);

  static void registerMessageCallback(MessageCallback cb) {
    _message_callback = cb;
  }
  static void registerAvailableCallback(AvailableCallback cb) {
    _available_callback = cb;
  }

  static void _onPush(const rpc::payload::MailboxPush& msg);
  static void _onReadResponse(const rpc::payload::MailboxReadResponse& msg);
  static void _onAvailableResponse(const rpc::payload::MailboxAvailableResponse& msg);

  static void onLost() {}
  static void onSynchronized() {}

 private:
  static MessageCallback _message_callback;
  static AvailableCallback _available_callback;
};

using MailboxType = MailboxClass<>;
extern MailboxType Mailbox;

#endif
