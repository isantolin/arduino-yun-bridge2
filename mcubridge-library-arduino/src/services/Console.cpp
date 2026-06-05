#include "services/Console.h"

#include <etl/algorithm.h>

#include "Bridge.h"
#include "etl_ext/CounterIterator.h"

template <typename T>
ConsoleClass<T>::ConsoleClass() : _flags(0) {}

template <typename T>
void ConsoleClass<T>::begin() {
  _flags.set(BEGUN);
  _rx_buffer.clear();
  _tx_buffer.clear();
}

template <typename T>
void ConsoleClass<T>::_push(const rpc::payload::ConsoleWrite& msg) {
  const auto& data = msg.data;
  const size_t to_write = etl::min(static_cast<size_t>(data.size), _rx_buffer.available());
  using bridge::etl_ext::CounterIterator;
  etl::for_each(CounterIterator<size_t>(0U), CounterIterator<size_t>(to_write),
                [&](size_t i) { _rx_buffer.push(data.bytes[i]); });
}

template <typename T>
void ConsoleClass<T>::process() {
  if (!_tx_buffer.empty()) {
    rpc::payload::ConsoleWrite p;
    const size_t to_copy = etl::min(_tx_buffer.size(), sizeof(p.data.bytes));
    p.data.size = (pb_size_t)to_copy;
    if (to_copy > 0) {
      etl::copy_n(_tx_buffer.data(), to_copy, p.data.bytes);
    }
    if (Bridge.send(rpc::CommandId::CMD_CONSOLE_WRITE, 0, p)) {
      _tx_buffer.clear();
    }
  }
}

template <typename T>
size_t ConsoleClass<T>::write(uint8_t c) {
  if (_tx_buffer.full()) process();
  if (!_tx_buffer.full()) {
    _tx_buffer.push_back(c);
    return 1;
  }
  return 0;
}

template <typename T>
size_t ConsoleClass<T>::write(const uint8_t* buffer, size_t size) {
  if (buffer == nullptr || size == 0) return 0;
  size_t written = 0;
  using bridge::etl_ext::CounterIterator;
  const uint16_t max_chunks = static_cast<uint16_t>(size);
  [[maybe_unused]] auto _u1 = etl::find_if(
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

template <typename T>
int ConsoleClass<T>::available() { return static_cast<int>(_rx_buffer.size()); }

template <typename T>
int ConsoleClass<T>::read() {
  if (_rx_buffer.empty()) return -1;
  uint8_t c = _rx_buffer.front();
  _rx_buffer.pop();
  return static_cast<int>(c);
}

template <typename T>
int ConsoleClass<T>::peek() {
  if (_rx_buffer.empty()) return -1;
  return static_cast<int>(_rx_buffer.front());
}

template class ConsoleClass<void>;
ConsoleType Console;
