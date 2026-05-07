#ifndef MCUBRIDGE_ITERATORS_H
#define MCUBRIDGE_ITERATORS_H

#include <stddef.h>
#include <stdint.h>
#include <etl/iterator.h>

namespace bridge {
namespace utils {

/**
 * @brief Zero-overhead numeric iterator to replace manual for/while loops.
 * Enables using etl/algorithm without allocating dummy arrays in the stack.
 * [SIL-2] 100% STL-free implementation.
 */
class CounterIterator {
 public:
  using iterator_category = etl::input_iterator_tag;
  using value_type = uint32_t;
  using difference_type = ptrdiff_t;
  using pointer = const uint32_t*;
  using reference = uint32_t;

  explicit CounterIterator(uint32_t value) : _value(value) {}

  reference operator*() const { return _value; }
  CounterIterator& operator++() {
    ++_value;
    return *this;
  }
  CounterIterator operator++(int) {
    CounterIterator tmp(*this);
    ++_value;
    return tmp;
  }

  bool operator==(const CounterIterator& other) const {
    return _value == other._value;
  }
  bool operator!=(const CounterIterator& other) const {
    return !(*this == other);
  }

 private:
  uint32_t _value;
};

}  // namespace utils
}  // namespace bridge

#endif
