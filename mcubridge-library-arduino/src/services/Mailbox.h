#ifndef SERVICES_MAILBOX_H
#define SERVICES_MAILBOX_H

#include "config/bridge_config.h"

#if BRIDGE_ENABLE_MAILBOX
#include "etl/delegate.h"
#include "etl/span.h"
#include "etl/string_view.h"
#include "protocol/rpc_protocol.h"
#include "router/command_router.h"
#include "etl/message_router.h"

class MailboxClass : public etl::imessage_router {
 public:
  using MailboxHandler = etl::delegate<void(etl::span<const uint8_t>)>;
  using MailboxAvailableHandler = etl::delegate<void(uint16_t)>;

  MailboxClass();

  // [SIL-2] imessage_router interface
  void receive(const etl::imessage& msg) override;
  bool accepts(etl::message_id_t id) const override;
  bool is_null_router() const override { return false; }
  bool is_producer() const override { return true; }
  bool is_consumer() const override { return true; }

  void send(etl::string_view message);
  void send(etl::span<const uint8_t> data);
  void requestRead();
  void requestAvailable();

  inline void onMailboxMessage(MailboxHandler handler) { _mailbox_handler = handler; }
  inline void onMailboxAvailableResponse(MailboxAvailableHandler handler) { _mailbox_available_handler = handler; }

  void _onIncomingData(etl::span<const uint8_t> data);

  MailboxHandler _mailbox_handler;
  MailboxAvailableHandler _mailbox_available_handler;
};

extern MailboxClass Mailbox;
#endif
#endif
