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

template <typename T, size_t MAX_SIZE>
class CircularBufferWrapper : public etl::circular_buffer<T, MAX_SIZE> {
 public:
  using etl::circular_buffer<T, MAX_SIZE>::circular_buffer;

  template <typename TIterator>
  void assign(TIterator first, TIterator last) {
    this->clear();
    this->push(first, last);
  }
};

class MailboxClass : public BridgeObserver {
 public:

  MailboxClass();
  static void push(etl::span<const uint8_t> data);
  static void requestRead();
  static void requestAvailable();
  static void signalProcessed();

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
  CircularBufferWrapper<uint8_t, bridge::config::MAILBOX_RX_BUFFER_SIZE>
      _rx_buffer;
  uint16_t _available_count;
};

extern MailboxClass Mailbox;

#endif
