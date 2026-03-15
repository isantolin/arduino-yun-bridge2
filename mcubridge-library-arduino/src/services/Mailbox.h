#ifndef SERVICES_MAILBOX_H
#define SERVICES_MAILBOX_H

#include <stdint.h>
#include "config/bridge_config.h"
#undef min
#undef max
#include "etl/circular_buffer.h"
#include "etl/delegate.h"
#include "etl/span.h"
#include "protocol/BridgeEvents.h"
#include "protocol/rpc_structs.h"

#if defined(BRIDGE_HOST_TEST)
namespace bridge { namespace test { class MailboxTestAccessor; } }
#endif

class MailboxClass : public BridgeObserver {
#if defined(BRIDGE_HOST_TEST)
  friend class bridge::test::MailboxTestAccessor;
#endif
 public:
  using MailboxHandler = etl::delegate<void(etl::span<const uint8_t>)>;
  using MailboxAvailableHandler = etl::delegate<void(uint16_t)>;

  MailboxClass();

  // [SIL-2] Observer Interface
  void notification(MsgBridgeSynchronized) override { /* ready */ }
  void notification(MsgBridgeLost) override { _rx_buffer.clear(); }

  void write(etl::span<const uint8_t> data);
  void send(etl::span<const uint8_t> data) { write(data); }
  void requestRead();
  void requestAvailable();

  void onMailboxMessage(MailboxHandler handler) { _mailbox_handler = handler; }
  void onMailboxAvailable(MailboxAvailableHandler handler) { _available_handler = handler; }

  void _onIncomingData(etl::span<const uint8_t> data);
  void _onResponse(const rpc::payload::MailboxReadResponse& msg);
  void _onAvailableResponse(const rpc::payload::MailboxAvailableResponse& msg);

 private:
  MailboxHandler _mailbox_handler;
  MailboxAvailableHandler _available_handler;

  // [SIL-2] Use ETL containers for safe buffer management
  etl::circular_buffer<uint8_t, bridge::config::MAILBOX_RX_BUFFER_SIZE> _rx_buffer;
};

#if BRIDGE_ENABLE_MAILBOX
extern MailboxClass Mailbox;
#endif

#endif
