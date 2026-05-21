#include <iostream>
struct MyIterator {
  int val;
  bool operator!=(const MyIterator& o) const { return val != o.val; }
  MyIterator& operator++() {
    ++val;
    return *this;
  }
  int operator*() const { return val; }
};
int main() { return 0; }
