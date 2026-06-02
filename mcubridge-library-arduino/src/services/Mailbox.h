#ifndef SERVICES_MAILBOX_H
#define SERVICES_MAILBOX_H

#include "config/bridge_config.h"
#undef min
#undef max
#include <etl/circular_buffer.h>
#include <etl/delegate.h>
#include <etl/span.h>


#include "protocol/rpc_structs.h"



class MailboxClass {
 public:

  MailboxClass();
  static void push(etl::span<const uint8_t> data);
  static void requestRead();
  static void requestAvailable();
  static void signalProcessed();

  void _onIncomingData(const rpc::payload::MailboxPush& msg);
  void _onIncomingData(const rpc::payload::MailboxReadResponse& msg);
  void _onAvailableResponse(const rpc::payload::MailboxAvailableResponse& msg);

  void onLost() {
    _rx_buffer.clear();
    _available_count = 0U;
  }

 private:
  void _setIncomingData(etl::span<const uint8_t> data);
  etl::circular_buffer<uint8_t, bridge::config::MAILBOX_RX_BUFFER_SIZE>
      _rx_buffer;
  uint16_t _available_count;
};

extern MailboxClass Mailbox;

#endif
