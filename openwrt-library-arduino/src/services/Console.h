#ifndef SERVICES_CONSOLE_H
#define SERVICES_CONSOLE_H

#include "config/bridge_config.h"
#include <Stream.h>
#undef min
#undef max
#include "etl/circular_buffer.h"
#include "etl/vector.h"
#include "etl/span.h"

#if defined(BRIDGE_HOST_TEST)
namespace bridge {
namespace test {
  class ConsoleTestAccessor;
}
}
#endif

#include "protocol/BridgeEvents.h"

class ConsoleClass : public Stream, public BridgeObserver {
  #if defined(BRIDGE_HOST_TEST)
  friend class bridge::test::ConsoleTestAccessor;
  #endif
 public:
  ConsoleClass();
  void begin();
  
  // [SIL-2] Observer Interface
  void notification(MsgBridgeSynchronized) override { begin(); }
  void notification(MsgBridgeLost) override { _begun = false; }
  
  size_t write(uint8_t c) override;
  size_t write(const uint8_t *buffer, size_t size) override;
  
  void _push(etl::span<const uint8_t> data);
  
  int available() override;
  int read() override;
  int peek() override;
  void flush() override;

 private:
  bool _begun;
  bool _xoff_sent;
  
  // [SIL-2] Use ETL containers for safe buffer management
  etl::circular_buffer<uint8_t, BRIDGE_CONSOLE_RX_BUFFER_SIZE> _rx_buffer;
  etl::vector<uint8_t, BRIDGE_CONSOLE_TX_BUFFER_SIZE> _tx_buffer;
};

extern ConsoleClass Console;

#endif
