#ifndef SERVICES_MAILBOX_H
#define SERVICES_MAILBOX_H

#include "config/bridge_config.h"
#undef min
#undef max
#include <etl/circular_buffer.h>
#include <etl/span.h>
#include "protocol/BridgeEvents.h"
#include "protocol/rpc_structs.h"

class MailboxClass : public BridgeObserver {
 public:
  MailboxClass();
  static void push(etl::span<const uint8_t> data);
  [[maybe_unused]] static void requestRead();
  [[maybe_unused]] static void requestAvailable();
  [[maybe_unused]] static void signalProcessed();

  void _onIncomingData(const rpc::payload::MailboxPush& msg);
  void _onIncomingData(const rpc::payload::MailboxReadResponse& msg);
  static void _onAvailableResponse(const rpc::payload::MailboxAvailableResponse& msg);

  void notification(MsgBridgeSynchronized) override { /* ready */ }
  void notification(MsgBridgeLost) override { _rx_buffer.clear(); }

 private:
  etl::circular_buffer<uint8_t, bridge::config::MAILBOX_RX_BUFFER_SIZE> _rx_buffer;
};

extern MailboxClass Mailbox;

#endif
