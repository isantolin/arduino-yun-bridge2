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

  MailboxClass();
  static void push(etl::span<const uint8_t> data);
  static void requestRead();
  static void requestAvailable();
  static void signalProcessed();

  static void _onIncomingData(const rpc::payload::MailboxPush& msg);
  static void _onIncomingData(const rpc::payload::MailboxReadResponse& msg);
  static void _onAvailableResponse(const rpc::payload::MailboxAvailableResponse& msg);

  static void onLost() {}
  static void onSynchronized() {}
};

using MailboxType = MailboxClass<>;
extern MailboxType Mailbox;

#endif
