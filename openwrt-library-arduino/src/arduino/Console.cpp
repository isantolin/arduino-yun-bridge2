#include "Bridge.h"

// Note: <limits.h> removed - INT_MAX check replaced with explicit size_t cast

#include "protocol/rpc_protocol.h"
#include <etl/algorithm.h>

ConsoleClass::ConsoleClass()
    : _begun(false),
      _xoff_sent(false),
      _rx_buffer(),
      _tx_buffer() {
  // ETL containers initialize themselves
}

void ConsoleClass::begin() {
  _begun = true;
  _xoff_sent = false;
  _rx_buffer.clear();
  _tx_buffer.clear();
}

size_t ConsoleClass::write(uint8_t c) {
  if (!_begun) return 0;

  if (_tx_buffer.full()) {
    flush();
  }

  // Double check full in case flush didn't clear (e.g. comm failure)
  if (!_tx_buffer.full()) {
    _tx_buffer.push_back(c);
  } else {
    return 0; // Should not happen if flush works, but safe return
  }

  if (_tx_buffer.full() || c == '\n') {
    flush();
  }
  return 1;
}

size_t ConsoleClass::write(const uint8_t* buffer, size_t size) {
  if (!_begun) return 0;

  // If there's buffered data, flush it first to maintain order
  if (!_tx_buffer.empty()) {
    flush();
  }

  size_t remaining = size;
  size_t offset = 0;
  size_t transmitted = 0;
  while (remaining > 0) {
    size_t chunk_size =
        remaining > rpc::MAX_PAYLOAD_SIZE ? rpc::MAX_PAYLOAD_SIZE : remaining;
    if (!Bridge.sendFrame(
            rpc::CommandId::CMD_CONSOLE_WRITE,
            buffer + offset, chunk_size)) {
      break;
    }
    offset += chunk_size;
    remaining -= chunk_size;
    transmitted += chunk_size;
  }
  return transmitted;
}

int ConsoleClass::available() {
  if (_rx_buffer.empty()) {
    return 0;
  }
  return static_cast<int>(_rx_buffer.size());
}

int ConsoleClass::peek() {
  if (_rx_buffer.empty()) return -1;
  return _rx_buffer.front();
}

int ConsoleClass::read() {
  if (_rx_buffer.empty()) return -1;
  
  uint8_t c = _rx_buffer.front();
  _rx_buffer.pop();

  // High/Low watermark logic for XON/XOFF
  const size_t capacity = _rx_buffer.capacity();
  const size_t low_water = (capacity * 1) / 4;
  
  if (_xoff_sent && _rx_buffer.size() < low_water) {
    if (Bridge.sendFrame(rpc::CommandId::CMD_XON)) {
      _xoff_sent = false;
    }
  }

  return c;
}

void ConsoleClass::flush() {
  if (!_begun) {
    return;
  }
  
  if (!_tx_buffer.empty()) {
    size_t remaining = _tx_buffer.size();
    size_t offset = 0;
    const uint8_t* data_ptr = _tx_buffer.data();

    while (remaining > 0) {
      size_t chunk = remaining > rpc::MAX_PAYLOAD_SIZE ? rpc::MAX_PAYLOAD_SIZE : remaining;
      if (!Bridge.sendFrame(
              rpc::CommandId::CMD_CONSOLE_WRITE,
              data_ptr + offset, chunk)) {
        break;
      }
      offset += chunk;
      remaining -= chunk;
    }
    _tx_buffer.clear();
  }

  Bridge.flushStream();
}

void ConsoleClass::_push(const uint8_t* data, size_t length) {
  if (_rx_buffer.capacity() == 0 || length == 0) {
    return;
  }

  // [SIL-2] Calculate available space first, then copy deterministically
  // Standard Arduino Serial behavior: drop new data if buffer full
  const size_t available = _rx_buffer.capacity() - _rx_buffer.size();
  const size_t to_copy = etl::min(length, available);
  
  const uint8_t* const end = data + to_copy;
  while (data != end) {
    _rx_buffer.push(*data++);
  }

  const size_t capacity = _rx_buffer.capacity();
  const size_t high_water = (capacity * 3) / 4;
  
  if (!_xoff_sent && _rx_buffer.size() > high_water) {
    if (Bridge.sendFrame(rpc::CommandId::CMD_XOFF)) {
        _xoff_sent = true;
    }
  }
}
