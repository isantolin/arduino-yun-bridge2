#include <Arduino.h>
#include <Bridge.h>
#include <etl/array.h>
#include <pw_stream/memory_stream.h>
#include <pw_transfer/handler.h>
#include <services/EepromTransfer.h>

#if !defined(__AVR_ATmega2560__)
#error "This integration sketch requires Arduino Mega 2560 (ATmega2560)."
#endif

namespace {

static_assert(bridge::services::kEepromTransferResourceSize == 4096U, "Mega 2560 EEPROM resource must expose all 4 KiB");
static_assert(bridge::services::kEepromTransferResourceId == 0x4D454741UL, "EEPROM transfer resource ID is a stable wire contract");

constexpr uint32_t kLocalResourceId = 0x1001;
constexpr uint32_t kRemoteResourceId = 0x1002;

etl::array<pw::byte, 32> resource_bytes{};
pw::stream::MemoryReader resource_reader{
    pw::ConstByteSpan(resource_bytes.data(), resource_bytes.size())};
pw::transfer::ReadOnlyHandler resource_handler{
    kLocalResourceId, resource_reader};

etl::array<pw::byte, 64> client_output_bytes{};
pw::stream::MemoryWriter client_output{
    pw::ByteSpan(client_output_bytes.data(), client_output_bytes.size())};

void OnClientReadComplete(pw::Status status) {
  (void)status;
}

// Retain a real upstream Client::Read call in this compile-only sketch. It is
// not started during normal execution because Bridge's one Thread<1, 1> may
// serve or initiate a transfer at a time, never both concurrently.
void StartRemoteRead() {
  auto result = Bridge.transferClient().Read(
      kRemoteResourceId, client_output, OnClientReadComplete);
  (void)result;
}

}  // namespace

void setup() {
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
  if (!Bridge.transferService().RegisterHandler(resource_handler)) {
    Bridge.enterSafeState();
  }
  if (millis() == 0xFFFFFFFFUL) {
    StartRemoteRead();
  }
}

void loop() {
  Bridge.process();
}
