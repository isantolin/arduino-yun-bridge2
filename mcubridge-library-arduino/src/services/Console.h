#ifndef SERVICES_CONSOLE_H
#define SERVICES_CONSOLE_H

#include <Stream.h>
#include "config/bridge_config.h"
#include "protocol/BridgeEvents.h"
#include "router/command_router.h"
#include "etl/message_router.h"
#include "etl/circular_buffer.h"
#include "etl/span.h"
#include "etl/vector.h"

class ConsoleClass : public Stream,
                     public BridgeObserver,
                     public etl::imessage_router {
 public:
  ConsoleClass();
  void begin();

  // [SIL-2] imessage_router interface
  void receive(const etl::imessage& msg) override;
  bool accepts(etl::message_id_t id) const override;
  bool is_null_router() const override { return false; }
  bool is_producer() const override { return true; }
  bool is_consumer() const override { return true; }

  // [SIL-2] Observer Interface
  void notification(MsgBridgeSynchronized) override { begin(); }
  void notification(MsgBridgeLost) override { _begun = false; }

  size_t write(uint8_t c) override;
  size_t write(const uint8_t* buffer, size_t size) override;
  void _push(etl::span<const uint8_t> data);
  int available() override;
  int read() override;
  int peek() override;
  void flush() override;

  bool _begun;
  bool _xoff_sent;
  etl::circular_buffer<uint8_t, BRIDGE_CONSOLE_RX_BUFFER_SIZE> _rx_buffer;
  etl::vector<uint8_t, BRIDGE_CONSOLE_TX_BUFFER_SIZE> _tx_buffer;
};

extern ConsoleClass Console;
#endif
