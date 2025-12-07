#include "Bridge.h"

#include "protocol/rpc_protocol.h"

using namespace rpc;

ConsoleClass::ConsoleClass()
    : _begun(false),
      _rx_buffer_head(0),
      _rx_buffer_tail(0),
      _xoff_sent(false),
      _tx_buffer_pos(0) {}

void ConsoleClass::begin() {
  _begun = true;
  _rx_buffer_head = 0;
  _rx_buffer_tail = 0;
  _xoff_sent = false;
  _tx_buffer_pos = 0;
}

size_t ConsoleClass::write(uint8_t c) {
  if (!_begun) return 0;
  _tx_buffer[_tx_buffer_pos++] = c;
  
  if (_tx_buffer_pos >= CONSOLE_TX_BUFFER_SIZE || c == '\n') {
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
    if (!Bridge.sendFrame(CMD_CONSOLE_WRITE, buffer + offset, chunk_size)) {
      break;
    }
    offset += chunk_size;
    remaining -= chunk_size;
    transmitted += chunk_size;
  }
  return transmitted;
}

int ConsoleClass::available() {
  return (_rx_buffer_head - _rx_buffer_tail + CONSOLE_RX_BUFFER_SIZE) %
         CONSOLE_RX_BUFFER_SIZE;
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
    Bridge.sendFrame(CMD_XON, nullptr, 0);
    _xoff_sent = false;
  }

  return c;
}

void ConsoleClass::flush() {
  if (!_begun) {
    return;
  }
  
  if (_tx_buffer_pos > 0) {
    Bridge.sendFrame(CMD_CONSOLE_WRITE, _tx_buffer, _tx_buffer_pos);
    _tx_buffer_pos = 0;
  }

  Bridge.flushStream();
}

void ConsoleClass::_push(const uint8_t* buffer, size_t size) {
  for (size_t i = 0; i < size; i++) {
    uint16_t next_head = (_rx_buffer_head + 1) % CONSOLE_RX_BUFFER_SIZE;
    if (next_head != _rx_buffer_tail) {
      _rx_buffer[_rx_buffer_head] = buffer[i];
      _rx_buffer_head = next_head;
    }
  }

  if (!_xoff_sent && available() > CONSOLE_BUFFER_HIGH_WATER) {
    Bridge.sendFrame(CMD_XOFF, nullptr, 0);
    _xoff_sent = true;
  }
}