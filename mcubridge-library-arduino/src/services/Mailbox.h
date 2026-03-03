#ifndef SERVICES_MAILBOX_H
#define SERVICES_MAILBOX_H

#include "config/bridge_config.h"

#if BRIDGE_ENABLE_MAILBOX
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
};

extern MailboxClass Mailbox;
#endif

#endif
