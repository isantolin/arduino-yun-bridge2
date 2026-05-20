#ifndef MCUBRIDGE_ITERATORS_H
#define MCUBRIDGE_ITERATORS_H

#include <stddef.h>
#include <stdint.h>
#include <etl/iterator.h>

namespace bridge {
namespace etl_ext {

/**
 * @brief Zero-overhead numeric iterator to replace manual for/while loops.
 * Enables using etl/algorithm without allocating dummy arrays in the stack.
 * [SIL-2] 100% STL-free implementation.
 */
template <typename T>
class CounterIterator {
 public:
  using iterator_category = etl::input_iterator_tag;
  using value_type = T;
  using difference_type = ptrdiff_t;
  using pointer = const T*;
  using reference = T;

  explicit CounterIterator(T value) : _value(value) {}

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
  T _value;
};

}  // namespace etl_ext
}  // namespace bridge

#endif
