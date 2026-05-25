#include "services/Console.h"

#include <etl/algorithm.h>

#include "Bridge.h"
#include "etl_ext/CounterIterator.h"

ConsoleClass::ConsoleClass() : _flags(0) {}

void ConsoleClass::begin() {
  _flags.set(BEGUN);
  _rx_buffer.clear();
  _tx_buffer.clear();
}

void ConsoleClass::_push(const rpc::payload::ConsoleWrite& msg) {
  const auto& data = msg.data;
  const size_t to_write = etl::min(static_cast<size_t>(data.size), _rx_buffer.available());
  using bridge::etl_ext::CounterIterator;
  etl::for_each(CounterIterator<size_t>(0U), CounterIterator<size_t>(to_write),
                [&](size_t i) { _rx_buffer.push(data.bytes[i]); });
}

void ConsoleClass::process() {
  if (!_tx_buffer.empty()) {
    rpc::payload::ConsoleWrite p;
    rpc::payload::copy_to_pb_bytes(p.data, _tx_buffer.data(),
                                   _tx_buffer.size());
    if (Bridge.send(rpc::CommandId::CMD_CONSOLE_WRITE, 0, p)) {
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
  size_t written = 0;
  using bridge::etl_ext::CounterIterator;
  const uint16_t max_chunks = static_cast<uint16_t>(size);
  (void)etl::find_if(
      CounterIterator<uint16_t>(0U),
      CounterIterator<uint16_t>(max_chunks + 1U),
      [&](uint16_t) {
        if (_tx_buffer.full()) process();
        if (_tx_buffer.full()) return true;
        const size_t to_write = etl::min(size - written, _tx_buffer.available());
        _tx_buffer.insert(_tx_buffer.end(), buffer + written, buffer + written + to_write);
        written += to_write;
        return written >= size;
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

ConsoleClass Console;
