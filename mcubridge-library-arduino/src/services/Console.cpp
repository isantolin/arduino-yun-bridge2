#include "services/Console.h"
#include "Bridge.h"
#include <etl/algorithm.h>

ConsoleClass::ConsoleClass() : _flags(0) {}

void ConsoleClass::begin() {
  _flags.set(BEGUN);
  _rx_buffer.clear();
  _tx_buffer.clear();
}

void ConsoleClass::_push(const rpc::payload::ConsoleWrite& msg) {
  const auto& data = msg.data;
  etl::for_each(data.begin(), data.end(), [this](uint8_t b) {
    if (!_rx_buffer.full()) _rx_buffer.push(b);
  });
}

void ConsoleClass::process() {
  if (!_tx_buffer.empty()) {
    rpc::payload::ConsoleWrite msg = {};
    msg.data = etl::span<const uint8_t>(_tx_buffer.data(), _tx_buffer.size());
    if (Bridge.send(rpc::CommandId::CMD_CONSOLE_WRITE, 0, msg)) {
      _tx_buffer.clear();
    }
  }
}

size_t ConsoleClass::write(uint8_t c) {
  if (_tx_buffer.full()) process();
  if (!_tx_buffer.full()) {
    _tx_buffer.push_back(c);
    return 1;
  }
  return 0;
}

size_t ConsoleClass::write(const uint8_t* buffer, size_t size) {
  if (buffer == nullptr || size == 0) return 0;
  etl::span<const uint8_t> data(buffer, size);
  size_t written = 0;
  etl::for_each(data.begin(), data.end(), [this, &written](uint8_t b) {
    written += write(b);
  });
  return written;
}

int ConsoleClass::available() { return static_cast<int>(_rx_buffer.size()); }

int ConsoleClass::read() {
  if (_rx_buffer.empty()) return -1;
  uint8_t c = _rx_buffer.front();
  _rx_buffer.pop();
  return static_cast<int>(c);
}

int ConsoleClass::peek() {
  if (_rx_buffer.empty()) return -1;
  return static_cast<int>(_rx_buffer.front());
}

#ifndef BRIDGE_TEST_NO_GLOBALS
ConsoleClass Console;
#endif
