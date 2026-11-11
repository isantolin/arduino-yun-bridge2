#ifndef SERVICES_CONSOLE_H
#define SERVICES_CONSOLE_H

#include <Stream.h>
#include <etl/circular_buffer.h>
#include <etl/vector.h>

#include "Bridge.h"

class ConsoleClass : public Stream, public bridge::BridgeObserver {
 public:
  ConsoleClass() = default;
  void begin();
  void pushBytes(const uint8_t* data, size_t size);
  void process();

  void onLost() override {
    _rx_buffer.clear();
    _tx_buffer.clear();
  }

  // Stream implementation
  size_t write(uint8_t c) override;
  size_t write(const uint8_t* buffer, size_t size) override;
  int available() override;
  int read() override;
  int peek() override;
  void flush() override {}

 private:
  etl::circular_buffer<uint8_t, bridge::config::CONSOLE_RX_BUFFER_SIZE>
      _rx_buffer;
  etl::vector<uint8_t, bridge::config::CONSOLE_TX_BUFFER_SIZE> _tx_buffer;
};

extern ConsoleClass Console;

#endif
