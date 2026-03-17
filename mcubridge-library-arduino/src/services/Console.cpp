#include "Console.h"
#include "Bridge.h"
#include "util/pb_copy.h"

ConsoleClass::ConsoleClass() { _flags.reset(); }

void ConsoleClass::begin() {
  _rx_buffer.clear();
  _tx_buffer.clear();
  _flags.set(BEGUN);
  _flags.reset(XOFF_SENT);
}

void ConsoleClass::_push(etl::span<const uint8_t> data) {
  if (!_flags.test(BEGUN)) return;
  BRIDGE_ATOMIC_BLOCK {
    const size_t space = _rx_buffer.capacity() - _rx_buffer.size();
    const size_t to_copy = etl::min(data.size(), space);
    _rx_buffer.push(data.begin(), data.begin() + to_copy);
  }
}

size_t ConsoleClass::write(uint8_t c) {
  if (!_flags.test(BEGUN)) return 0;
  BRIDGE_ATOMIC_BLOCK {
    if (_tx_buffer.full()) flush();
    _tx_buffer.push_back(c);
  }
  return 1;
}

size_t ConsoleClass::write(const uint8_t* buffer, size_t size) {
  if (!_flags.test(BEGUN) || !buffer || size == 0) return 0;

  size_t sent = 0;
  while (sent < size) {
    size_t remaining;
    BRIDGE_ATOMIC_BLOCK { remaining = _tx_buffer.capacity() - _tx_buffer.size(); }

    if (remaining == 0) {
      flush();
      BRIDGE_ATOMIC_BLOCK { remaining = _tx_buffer.capacity() - _tx_buffer.size(); }
      if (remaining == 0) break; // Could not clear space
    }

    size_t to_copy = etl::min(size - sent, remaining);
    BRIDGE_ATOMIC_BLOCK {
      _tx_buffer.insert(_tx_buffer.end(), buffer + sent, buffer + sent + to_copy);
    }
    sent += to_copy;
  }
  return sent;
}

int ConsoleClass::available() {
  int count = 0;
  BRIDGE_ATOMIC_BLOCK { count = static_cast<int>(_rx_buffer.size()); }
  return count;
}

int ConsoleClass::read() {
  uint8_t c = 0;
  bool empty = true;
  BRIDGE_ATOMIC_BLOCK {
    empty = _rx_buffer.empty();
    if (!empty) {
      c = _rx_buffer.front();
      _rx_buffer.pop();
      if (_flags.test(XOFF_SENT) && _rx_buffer.size() <= _rx_buffer.capacity() / 2) {
        _flags.reset(XOFF_SENT);
      }
    }
  }
  return empty ? -1 : static_cast<int>(c);
}

int ConsoleClass::peek() {
  uint8_t c = 0;
  bool empty = true;
  BRIDGE_ATOMIC_BLOCK {
    empty = _rx_buffer.empty();
    if (!empty) c = _rx_buffer.front();
  }
  return empty ? -1 : static_cast<int>(c);
}

void ConsoleClass::flush() {
  if (!_flags.test(BEGUN)) return;
  etl::span<const uint8_t> data;
  BRIDGE_ATOMIC_BLOCK {
    if (_tx_buffer.empty()) return;
    data = etl::span<const uint8_t>(_tx_buffer.data(), _tx_buffer.size());
  }
  rpc::payload::ConsoleWrite msg = {};
  rpc::util::pb_setup_encode_span(msg.data, data);
  if (Bridge.sendPbCommand(rpc::CommandId::CMD_CONSOLE_WRITE, msg)) {
    BRIDGE_ATOMIC_BLOCK { _tx_buffer.clear(); }
  }
}
