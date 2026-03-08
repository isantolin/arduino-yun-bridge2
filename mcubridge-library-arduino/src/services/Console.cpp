#include <etl/algorithm.h>

#include "Bridge.h"
#include "protocol/rpc_protocol.h"

/// XON resume threshold: fraction of buffer capacity (numerator / denominator).
static constexpr size_t kLowWaterNumerator = 1;
/// XOFF pause threshold: fraction of buffer capacity (numerator / denominator).
static constexpr size_t kHighWaterNumerator = 3;
/// Common denominator for both watermark thresholds.
static constexpr size_t kWatermarkDenominator = 4;

ConsoleClass::ConsoleClass()
    : etl::imessage_router(rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE)),
      _begun(false),
      _xoff_sent(false),
      _rx_buffer(),
      _tx_buffer() {
  // ETL containers initialize themselves
}

void ConsoleClass::receive(const etl::imessage& msg) {
  if (msg.get_message_id() != rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE)) return;
  const auto& cmd_msg = static_cast<const bridge::router::CommandMessage&>(msg);
  Bridge._withPayloadAck<rpc::payload::ConsoleWrite>(
      cmd_msg, [this](const rpc::payload::ConsoleWrite& pl) {
        _push(etl::span<const uint8_t>(pl.data, pl.length));
      });
}

bool ConsoleClass::accepts(etl::message_id_t id) const {
  return id == rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);
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

  if (!_tx_buffer.full()) {
    _tx_buffer.push_back(c);
  } else {
    return 0;
  }

  if (_tx_buffer.full() || c == '\n') {
    flush();
  }
  return 1;
}

size_t ConsoleClass::write(const uint8_t* buffer, size_t size) {
  if (!_begun || size == 0) return 0;

  if (!_tx_buffer.empty()) {
    flush();
  }

  if (Bridge.sendChunkyFrame(rpc::CommandId::CMD_CONSOLE_WRITE,
                             etl::span<const uint8_t>(),
                             etl::span<const uint8_t>(buffer, size))) {
    return size;
  }
  return 0;
}

int ConsoleClass::available() {
  int size = 0;
  BRIDGE_ATOMIC_BLOCK {
    size = static_cast<int>(_rx_buffer.size());
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

      const size_t capacity = _rx_buffer.capacity();
      const size_t low_water =
          (capacity * kLowWaterNumerator) / kWatermarkDenominator;

      if (_xoff_sent && _rx_buffer.size() < low_water) {
        xon_needed = true;
      }
    }
  }

  if (xon_needed) {
    if (Bridge.sendFrame(rpc::CommandId::CMD_XON)) {
      BRIDGE_ATOMIC_BLOCK { _xoff_sent = false; }
    }
  }

  return c;
}

void ConsoleClass::flush() {
  if (!_begun) {
    return;
  }

  if (!_tx_buffer.empty()) {
    if (Bridge.sendChunkyFrame(
            rpc::CommandId::CMD_CONSOLE_WRITE, etl::span<const uint8_t>(),
            etl::span<const uint8_t>(_tx_buffer.data(), _tx_buffer.size()))) {
      _tx_buffer.clear();
    }
  }

  Bridge.flushStream();
}

void ConsoleClass::_push(etl::span<const uint8_t> data) {
  if (data.empty()) return;

  bool xoff_needed = false;

  BRIDGE_ATOMIC_BLOCK {
    if (_rx_buffer.capacity() == 0) return;

    const size_t space = _rx_buffer.capacity() - _rx_buffer.size();
    const size_t to_copy = etl::min(data.size(), space);

    _rx_buffer.push(data.begin(), data.begin() + to_copy);

    const size_t capacity = _rx_buffer.capacity();
    const size_t high_water =
        (capacity * kHighWaterNumerator) / kWatermarkDenominator;

    if (!_xoff_sent && _rx_buffer.size() > high_water) {
      xoff_needed = true;
    }
  }

  if (xoff_needed) {
    if (Bridge.sendFrame(rpc::CommandId::CMD_XOFF)) {
      BRIDGE_ATOMIC_BLOCK { _xoff_sent = true; }
    }
  }
}

ConsoleClass Console;
