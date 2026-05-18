#ifndef SERVICES_MAILBOX_H
#define SERVICES_MAILBOX_H

#include "config/bridge_config.h"
#undef min
#undef max
#include <etl/circular_buffer.h>
#include <etl/delegate.h>
#include <etl/span.h>

#include "protocol/BridgeEvents.h"
#include "protocol/rpc_structs.h"

class MailboxClass : public BridgeObserver {
 public:
  using AvailableHandler = etl::delegate<void(uint16_t)>;

  MailboxClass();
  static void push(etl::span<const uint8_t> data);
  static void requestRead();
  static void requestAvailable();
  static void signalProcessed();
  void onAvailable(AvailableHandler handler) { _available_handler = handler; }
  uint16_t availableCount() const { return _available_count; }

  void _onIncomingData(const rpc::payload::MailboxPush& msg);
  void _onIncomingData(const rpc::payload::MailboxReadResponse& msg);
  void _onAvailableResponse(const rpc::payload::MailboxAvailableResponse& msg);

  void notification(MsgBridgeSynchronized) override { /* ready */ }
  void notification(MsgBridgeLost) override {
    _rx_buffer.clear();
    _available_count = 0U;
  }

 private:
  void _setIncomingData(etl::span<const uint8_t> data);
  etl::circular_buffer<uint8_t, bridge::config::MAILBOX_RX_BUFFER_SIZE>
      _rx_buffer;
  uint16_t _available_count;
  AvailableHandler _available_handler;
};

extern MailboxClass Mailbox;

#endif
