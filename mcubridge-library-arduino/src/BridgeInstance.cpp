// [SIL-2] Default global Bridge instance for production firmware.
// Excluded from host test builds, which substitute bridge_test_global.cpp.
#include "Bridge.h"

#if defined(MCUBRIDGE_RENODE_UART_SHIM)
#if !defined(ARDUINO_ARCH_SAMD)
#error "MCUBRIDGE_RENODE_UART_SHIM is only valid for SAMD21 Renode emulation"
#endif

#include <stdint.h>

namespace {

// The Renode SAMD5_UART model used by the SAMD21 platform implements the
// interrupt flags and DATA register, but not the rest of the SERCOM register
// map. This CI-only Stream preserves byte-level UART traffic without touching
// unimplemented control/status registers. Physical SAMD builds use Serial1.
class RenodeSAMD21Stream final : public Stream {
 public:
  int available() override {
    return _has_peeked || (registerAt(kInterruptFlagOffset) & kReceiveComplete) != 0U ? 1 : 0;
  }

  int read() override {
    const int value = peek();
    if (value >= 0) {
      _has_peeked = false;
    }
    return value;
  }

  int peek() override {
    if (_has_peeked) {
      return _peeked;
    }
    if ((registerAt(kInterruptFlagOffset) & kReceiveComplete) == 0U) {
      return -1;
    }
    _peeked = registerAt(kDataOffset);
    _has_peeked = true;
    return _peeked;
  }

  void flush() override {}

  size_t write(uint8_t value) override {
    registerAt(kDataOffset) = value;
    return 1U;
  }

  using Print::write;

 private:
  static constexpr uintptr_t kSercom5Base = 0x42001C00UL;
  static constexpr uintptr_t kInterruptFlagOffset = 0x18UL;
  static constexpr uintptr_t kDataOffset = 0x28UL;
  static constexpr uint8_t kReceiveComplete = 0x04U;

  static volatile uint8_t& registerAt(uintptr_t offset) {
    return *reinterpret_cast<volatile uint8_t*>(kSercom5Base + offset);
  }

  bool _has_peeked{false};
  int _peeked{-1};
};

RenodeSAMD21Stream renodeUartStream;

}  // namespace

BridgeClass Bridge(renodeUartStream);
#elif defined(SERIAL_PORT_LINUXBRIDGE)
BridgeClass Bridge(SERIAL_PORT_LINUXBRIDGE);
#elif defined(SERIAL_PORT_HARDWARE)
BridgeClass Bridge(SERIAL_PORT_HARDWARE);
#else
BridgeClass Bridge(Serial);
#endif
