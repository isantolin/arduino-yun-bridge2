#ifndef BRIDGE_BUFFER_VIEW_H
#define BRIDGE_BUFFER_VIEW_H

#include <cstddef>
#include <cstdint>

class BufferView {
 public:
  constexpr BufferView() : _data(nullptr), _size(0) {}
    constexpr BufferView(const uint8_t* data, std::size_t size)
      : _data(data), _size(size) {}

  template <std::size_t N>
  constexpr BufferView(const uint8_t (&array)[N])
      : _data(array), _size(N) {}

  constexpr const uint8_t* data() const { return _data; }
  constexpr std::size_t size() const { return _size; }
  constexpr bool empty() const { return _size == 0; }
  constexpr bool valid() const { return _size == 0 || _data != nullptr; }

  constexpr BufferView slice(std::size_t offset) const {
    return slice(offset, _size - offset);
  }

  constexpr BufferView slice(std::size_t offset, std::size_t count) const {
    if (offset >= _size) {
      return BufferView();
    }
    if (count > (_size - offset)) {
      count = _size - offset;
    }
    return BufferView(_data ? _data + offset : nullptr, count);
  }

 private:
  const uint8_t* _data;
  std::size_t _size;
};

#endif
