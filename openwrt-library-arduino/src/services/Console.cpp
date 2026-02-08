#include "Bridge.h"

#include "protocol/rpc_protocol.h"
#include <etl/algorithm.h>

/// XON resume threshold: fraction of buffer capacity (numerator / denominator).
static constexpr size_t kLowWaterNumerator = 1;
/// XOFF pause threshold: fraction of buffer capacity (numerator / denominator).
static constexpr size_t kHighWaterNumerator = 3;
/// Common denominator for both watermark thresholds.
static constexpr size_t kWatermarkDenominator = 4;

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
    
    // [SIL-2] Best Effort Delivery:
    // If the internal TX queue is full, we stop writing and return the number
    // of bytes successfully buffered to prevent blocking the main loop indefinitely.
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
  int size = 0;
  BRIDGE_ATOMIC_BLOCK {
    if (_rx_buffer.empty()) {
      size = 0;
    } else {
      size = static_cast<int>(_rx_buffer.size());
    }
  }
  return size;
}

int ConsoleClass::peek() {
  int c = -1;
  BRIDGE_ATOMIC_BLOCK {
    if (!_rx_buffer.empty()) {
      c = _rx_buffer.front();
    }
  }
  return c;
}

int ConsoleClass::read() {
  int c = -1;
  bool xon_needed = false;

  BRIDGE_ATOMIC_BLOCK {
    if (!_rx_buffer.empty()) {
      c = _rx_buffer.front();
      _rx_buffer.pop();

      // High/Low watermark logic for XON/XOFF
      const size_t capacity = _rx_buffer.capacity();
      const size_t low_water = (capacity * kLowWaterNumerator) / kWatermarkDenominator;
      
      if (_xoff_sent && _rx_buffer.size() < low_water) {
        xon_needed = true;
      }
    }
  }

  if (xon_needed) {
    if (Bridge.sendFrame(rpc::CommandId::CMD_XON)) {
      BRIDGE_ATOMIC_BLOCK {
        _xoff_sent = false;
      }
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
  if (length == 0) return;

  bool xoff_needed = false;

  BRIDGE_ATOMIC_BLOCK {
    if (_rx_buffer.capacity() == 0) return;

    // [SIL-2] Calculate available space first, then copy deterministically
    // Drop new data if buffer is full.
    const size_t available = _rx_buffer.capacity() - _rx_buffer.size();
    const size_t to_copy = etl::min(length, available);
    
    const uint8_t* const end = data + to_copy;
    while (data != end) {
      _rx_buffer.push(*data++);
    }

    const size_t capacity = _rx_buffer.capacity();
    const size_t high_water = (capacity * kHighWaterNumerator) / kWatermarkDenominator;
    
    if (!_xoff_sent && _rx_buffer.size() > high_water) {
      xoff_needed = true;
    }
  }

  if (xoff_needed) {
    if (Bridge.sendFrame(rpc::CommandId::CMD_XOFF)) {
        BRIDGE_ATOMIC_BLOCK {
          _xoff_sent = true;
        }
    }
  }
}
