#ifndef SERVICES_CONSOLE_H
#define SERVICES_CONSOLE_H

#include <Stream.h>

#include "config/bridge_config.h"
#undef min
#undef max
#include <etl/bitset.h>
#include <etl/circular_buffer.h>
#include <etl/span.h>
#include <etl/vector.h>

#include "protocol/BridgeEvents.h"

#if defined(BRIDGE_HOST_TEST)
namespace bridge { namespace test { class ConsoleTestAccessor; } }
#endif

class ConsoleClass : public Stream, public BridgeObserver {
#if defined(BRIDGE_HOST_TEST)
  friend class bridge::test::ConsoleTestAccessor;
#endif
 public:
  enum ConsoleFlag : uint8_t {
    BEGUN = 0,
    XOFF_SENT = 1,
    NUM_FLAGS = 2
  };

  ConsoleClass();
  void begin();
  [[maybe_unused]] bool isReady() const { return _flags.test(BEGUN); }

  // [SIL-2] Observer Interface
  void notification(MsgBridgeSynchronized) override { begin(); }
  void notification(MsgBridgeLost) override { _flags.reset(BEGUN); }

  size_t write(uint8_t c) override;
  size_t write(const uint8_t* buffer, size_t size) override;

  void _push(etl::span<const uint8_t> data);

  int available() override;
  int read() override;
  int peek() override;
  void flush() override;

 private:
  etl::bitset<NUM_FLAGS> _flags;

  // [SIL-2] Use ETL containers for safe buffer management
  etl::circular_buffer<uint8_t, bridge::config::CONSOLE_RX_BUFFER_SIZE> _rx_buffer;
  etl::vector<uint8_t, bridge::config::CONSOLE_TX_BUFFER_SIZE> _tx_buffer;
};

extern ConsoleClass Console;

#endif
