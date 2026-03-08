#ifndef SERVICES_MAILBOX_H
#define SERVICES_MAILBOX_H

#include "config/bridge_config.h"

#if BRIDGE_ENABLE_MAILBOX
#include "etl/circular_buffer.h"
#include "etl/delegate.h"
#include "etl/span.h"
#include "etl/string_view.h"
#include "protocol/rpc_protocol.h"

class BridgeClass;

class MailboxClass {
  friend class BridgeClass;

 public:
  using MailboxHandler = etl::delegate<void(etl::span<const uint8_t>)>;
  using MailboxAvailableHandler = etl::delegate<void(uint16_t)>;

  MailboxClass();

  void send(etl::string_view message);
  void send(etl::span<const uint8_t> data);
  void requestRead();
  void requestAvailable();

  // [SIL-2] Non-blocking buffer interface
  bool available() const { return !_rx_buffer.empty(); }
  size_t size() const { return _rx_buffer.size(); }
  void clear() { _rx_buffer.clear(); }

  inline void onMailboxMessage(MailboxHandler handler) {
    _mailbox_handler = handler;
  }
  inline void onMailboxAvailableResponse(MailboxAvailableHandler handler) {
    _mailbox_available_handler = handler;
  }

 private:
  void _onIncomingData(etl::span<const uint8_t> data);

  MailboxHandler _mailbox_handler;
  MailboxAvailableHandler _mailbox_available_handler;

  // [SIL-2] Static buffer for deterministic memory usage
  etl::circular_buffer<uint8_t, BRIDGE_MAILBOX_RX_BUFFER_SIZE> _rx_buffer;
};

extern MailboxClass Mailbox;
#endif

#endif
