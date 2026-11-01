#include "services/Console.h"

#include <etl/algorithm.h>

#include "Bridge.h"

void ConsoleClass::begin() {
  _rx_buffer.clear();
  _tx_buffer.clear();
}

void ConsoleClass::pushBytes(const uint8_t* data, size_t size) {
  if (data == nullptr || size == 0) return;
  const size_t to_write = etl::min(size, _rx_buffer.available());
  _rx_buffer.push(data, data + to_write);
}

void ConsoleClass::process() {
  if (!_tx_buffer.empty()) {
    if (Bridge.sendConsoleStream(
            etl::span<const uint8_t>(_tx_buffer.data(), _tx_buffer.size()))) {
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
  if (_tx_buffer.full()) process();
  const size_t to_write = etl::min(size, _tx_buffer.available());
  _tx_buffer.insert(_tx_buffer.end(), buffer, buffer + to_write);
  if (to_write < size) {
    process();
    const size_t extra_write =
        etl::min(size - to_write, _tx_buffer.available());
    _tx_buffer.insert(_tx_buffer.end(), buffer + to_write,
                      buffer + to_write + extra_write);
    return to_write + extra_write;
  }
  return to_write;
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

ConsoleClass Console;
