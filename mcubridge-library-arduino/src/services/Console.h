#ifndef SERVICES_CONSOLE_H
#define SERVICES_CONSOLE_H

#include <Stream.h>
#include "config/bridge_config.h"
#undef min
#undef max
#include <etl/circular_buffer.h>
#include <etl/vector.h>
#include <etl/bitset.h>
#include <etl/span.h>
#include "protocol/BridgeEvents.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"

class ConsoleClass : public Stream, public BridgeObserver {
 public:
  ConsoleClass();
  void begin();
  void _push(const rpc::payload::ConsoleWrite& msg);
  void process();

  void notification(MsgBridgeSynchronized) override { begin(); }
  void notification(MsgBridgeLost) override { _flags.reset(BEGUN); }

  // Stream implementation
  size_t write(uint8_t c) override;
  size_t write(const uint8_t* buffer, size_t size) override;
  int available() override;
  int read() override;
  int peek() override;
  void flush() override {}

 private:
  enum Flags { BEGUN = 0 };
  etl::bitset<1> _flags;
  etl::circular_buffer<uint8_t, bridge::config::CONSOLE_RX_BUFFER_SIZE> _rx_buffer;
  etl::vector<uint8_t, bridge::config::CONSOLE_TX_BUFFER_SIZE> _tx_buffer;
};

extern ConsoleClass Console;

#endif
