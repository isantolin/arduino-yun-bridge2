#include "Bridge.h"

#include <limits.h>

#include "protocol/rpc_protocol.h"

using namespace rpc;

ConsoleClass::ConsoleClass()
    : _begun(false),
      _rx_buffer_head(0),
      _rx_buffer_tail(0),
  _tx_buffer_pos(0),
  _xoff_sent(false) {}

void ConsoleClass::begin() {
  _begun = true;
  _rx_buffer_head = 0;
  _rx_buffer_tail = 0;
  _xoff_sent = false;
  _tx_buffer_pos = 0;
}

size_t ConsoleClass::write(uint8_t c) {
  if (!_begun) return 0;

  const size_t capacity = CONSOLE_TX_BUFFER_SIZE;
  if (capacity == 0) {
    return 0;
  }

  if (_tx_buffer_pos >= capacity) {
    flush();
  }

  if (_tx_buffer_pos < capacity) {
    _tx_buffer[_tx_buffer_pos++] = c;
  }

  if (_tx_buffer_pos >= capacity || c == '\n') {
    flush();
  }
  return 1;
}

size_t ConsoleClass::write(const uint8_t* buffer, size_t size) {
  if (!_begun) return 0;

  // If there's buffered data, flush it first to maintain order
  if (_tx_buffer_pos > 0) {
    flush();
  }

  size_t remaining = size;
  size_t offset = 0;
  size_t transmitted = 0;
  while (remaining > 0) {
    size_t chunk_size =
        remaining > MAX_PAYLOAD_SIZE ? MAX_PAYLOAD_SIZE : remaining;
    if (!Bridge.sendFrame(
            CommandId::CMD_CONSOLE_WRITE,
            BufferView(buffer + offset, chunk_size))) {
      break;
    }
    offset += chunk_size;
    remaining -= chunk_size;
    transmitted += chunk_size;
  }
  return transmitted;
}

int ConsoleClass::available() {
  const size_t capacity = CONSOLE_RX_BUFFER_SIZE;
  if (capacity == 0) {
    return 0;
  }

  const size_t head = _rx_buffer_head;
  const size_t tail = _rx_buffer_tail;
  size_t used = (head + capacity - tail) % capacity;
  if (head == tail) {
    used = 0;
  }
  if (used > static_cast<size_t>(INT_MAX)) {
    used = static_cast<size_t>(INT_MAX);
  }
  return static_cast<int>(used);
}

int ConsoleClass::peek() {
  if (_rx_buffer_head == _rx_buffer_tail) return -1;
  return _rx_buffer[_rx_buffer_tail];
}

int ConsoleClass::read() {
  if (_rx_buffer_head == _rx_buffer_tail) return -1;
  uint8_t c = _rx_buffer[_rx_buffer_tail];
  _rx_buffer_tail = (_rx_buffer_tail + 1) % CONSOLE_RX_BUFFER_SIZE;

  if (_xoff_sent && available() < CONSOLE_BUFFER_LOW_WATER) {
    Bridge.sendFrame(CommandId::CMD_XON);
    _xoff_sent = false;
  }

  return c;
}

void ConsoleClass::flush() {
  if (!_begun) {
    return;
  }
  
  if (_tx_buffer_pos > 0) {
    size_t remaining = _tx_buffer_pos;
    size_t offset = 0;
    while (remaining > 0) {
      size_t chunk = remaining > MAX_PAYLOAD_SIZE ? MAX_PAYLOAD_SIZE : remaining;
      if (!Bridge.sendFrame(
              CommandId::CMD_CONSOLE_WRITE,
              BufferView(_tx_buffer + offset, chunk))) {
        break;
      }
      offset += chunk;
      remaining -= chunk;
    }
    _tx_buffer_pos = 0;
  }

  Bridge.flushStream();
}

void ConsoleClass::_push(BufferView chunk) {
  const size_t capacity = CONSOLE_RX_BUFFER_SIZE;
  if (capacity == 0 || chunk.empty() || !chunk.valid()) {
    return;
  }

  const uint8_t* data = chunk.data();
  for (size_t i = 0; i < chunk.size(); i++) {
    size_t next_head = (_rx_buffer_head + 1) % capacity;
    if (next_head != _rx_buffer_tail) {
      _rx_buffer[_rx_buffer_head] = data[i];
      _rx_buffer_head = next_head;
    }
  }

  if (!_xoff_sent && available() > CONSOLE_BUFFER_HIGH_WATER) {
    Bridge.sendFrame(CommandId::CMD_XOFF);
    _xoff_sent = true;
  }
}