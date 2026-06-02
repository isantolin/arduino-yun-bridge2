#include "services/Console.h"
#include "Bridge.h"
#include "pb_encode.h"
#include "pb_decode.h"
#if BRIDGE_ENABLE_CONSOLE
ConsoleClass::ConsoleClass() : _rx_buffer(), _tx_buffer() {}
void ConsoleClass::begin() { _flags.set(BEGUN); }
void ConsoleClass::process() {
  if (!_tx_buffer.empty()) {
    rpc_pb_ConsoleWrite p = rpc_pb_ConsoleWrite_init_default;
    etl::span<const uint8_t> span(_tx_buffer.data(), _tx_buffer.size());
    p.data.funcs.encode = &BridgeClass::_encode_span_callback;
    p.data.arg = (void*)&span;
    if (Bridge.send(rpc::CommandId::CMD_CONSOLE_WRITE, 0, p)) { _tx_buffer.clear(); }
  }
}
size_t ConsoleClass::write(uint8_t c) { if (!_tx_buffer.full()) { _tx_buffer.push_back(c); return 1; } return 0; }
size_t ConsoleClass::write(const uint8_t* buffer, size_t size) { size_t n = 0; while (n < size) { if (write(buffer[n])) n++; else break; } return n; }
int ConsoleClass::available() { return _rx_buffer.size(); }
int ConsoleClass::read() { if (_rx_buffer.empty()) return -1; uint8_t b = 0; _rx_buffer.pop(b); return b; }
int ConsoleClass::peek() { if (_rx_buffer.empty()) return -1; return _rx_buffer.front(); }
void ConsoleClass::_put_rx(uint8_t c) { if (!_rx_buffer.full()) _rx_buffer.push(c); }
ConsoleClass Console;
#endif
