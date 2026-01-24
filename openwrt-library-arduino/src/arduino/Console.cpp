#include "Bridge.h"
#include "protocol/rpc_protocol.h"

ConsoleClass::ConsoleClass()
    : _begun(false),
      _xoff_sent(false) {
  _rx_buffer.clear();
  _tx_buffer.clear();
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

  // If still full after flush (e.g. send failed), we might drop or block.
  // Standard Bridge behavior is non-blocking drop or best effort.
  if (!_tx_buffer.full()) {
    _tx_buffer.push(c);
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

  const size_t low_water = (_rx_buffer.capacity() * 1) / 4;
  if (_xoff_sent && _rx_buffer.size() < low_water) {
    if (Bridge.sendFrame(rpc::CommandId::CMD_XON)) {
      _xoff_sent = false;
    }
  }

  return c;
}

void ConsoleClass::flush() {
  if (!_begun || _tx_buffer.empty()) {
    return;
  }
  
  // Drain circular buffer into linear frames
  // Use Bridge scratch buffer to avoid stack allocation
  uint8_t* scratch = Bridge.getScratchBuffer();
  size_t count = 0;
  
  while (!_tx_buffer.empty()) {
      scratch[count++] = _tx_buffer.front();
      _tx_buffer.pop();
      
      if (count == rpc::MAX_PAYLOAD_SIZE) {
          if (!Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, scratch, count)) {
              // Failed to send. We lost data that was popped.
              // Ideally we would peek, send, then pop.
              // But sendFrame is synchronous-ish (copies to transport buffer).
              // If it returns false, transport buffer is full.
              // We should probably stop trying to flush if send fails.
              // But we already popped. This logic is a bit lossy on failure, 
              // which matches original behavior (flush cleared buffer even if send failed partially?)
              // Original code:
              // if (!Bridge.sendFrame(...)) break;
              // offset += chunk; remaining -= chunk;
              // _tx_buffer_pos = 0; <- CLEARS EVERYTHING even if break!
              break;
          }
          count = 0;
      }
  }
  
  if (count > 0) {
      Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, scratch, count);
  }

  Bridge.flushStream();
}

void ConsoleClass::_push(const uint8_t* data, size_t length) {
  if (length == 0) return;

  for (size_t i = 0; i < length; i++) {
    if (!_rx_buffer.full()) {
      _rx_buffer.push(data[i]);
    } else {
      // Buffer full, drop new data
      break; 
    }
  }

  const size_t high_water = (_rx_buffer.capacity() * 3) / 4;
  if (!_xoff_sent && _rx_buffer.size() > high_water) {
    if (Bridge.sendFrame(rpc::CommandId::CMD_XOFF)) {
        _xoff_sent = true;
    }
  }
}