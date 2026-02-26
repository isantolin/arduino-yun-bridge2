#ifndef SERVICES_MAILBOX_H
#define SERVICES_MAILBOX_H

#include "config/bridge_config.h"

#if BRIDGE_ENABLE_MAILBOX
#include "etl/delegate.h"
#include "etl/string_view.h"
#include "protocol/rpc_protocol.h"

class BridgeClass;

class MailboxClass {
  friend class BridgeClass;

 public:
  using MailboxHandler = etl::delegate<void(const uint8_t*, uint16_t)>;
  using MailboxAvailableHandler = etl::delegate<void(uint16_t)>;

  MailboxClass();

  void send(etl::string_view message);
  void send(const uint8_t* data, size_t length);
  void requestRead();
  void requestAvailable();

  inline void onMailboxMessage(MailboxHandler handler) {
    _mailbox_handler = handler;
  }
  inline void onMailboxAvailableResponse(MailboxAvailableHandler handler) {
    _mailbox_available_handler = handler;
  }

 private:
  MailboxHandler _mailbox_handler;
  MailboxAvailableHandler _mailbox_available_handler;
};

extern MailboxClass Mailbox;
#endif

#endif
