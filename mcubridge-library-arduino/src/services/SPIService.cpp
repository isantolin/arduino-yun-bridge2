#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif
#include "SPIService.h"

#if BRIDGE_ENABLE_SPI
SPIServiceClass::SPIServiceClass() = default;

void SPIServiceClass::begin() { _initiator.Begin(); }

void SPIServiceClass::end() { _initiator.End(); }

pw::Status SPIServiceClass::setConfig(const rpc_pb_SpiConfig& config) {
  pw::spi::ClockPolarity polarity;
  pw::spi::ClockPhase phase;
  switch (config.data_mode) {
    case rpc_pb_SpiDataMode_SPI_DATA_MODE_0:
      polarity = pw::spi::ClockPolarity::kActiveHigh;
      phase = pw::spi::ClockPhase::kRisingEdge;
      break;
    case rpc_pb_SpiDataMode_SPI_DATA_MODE_1:
      polarity = pw::spi::ClockPolarity::kActiveHigh;
      phase = pw::spi::ClockPhase::kFallingEdge;
      break;
    case rpc_pb_SpiDataMode_SPI_DATA_MODE_2:
      polarity = pw::spi::ClockPolarity::kActiveLow;
      phase = pw::spi::ClockPhase::kRisingEdge;
      break;
    case rpc_pb_SpiDataMode_SPI_DATA_MODE_3:
      polarity = pw::spi::ClockPolarity::kActiveLow;
      phase = pw::spi::ClockPhase::kFallingEdge;
      break;
    default:
      return pw::Status::InvalidArgument();
  }

  pw::spi::BitOrder bit_order;
  switch (config.bit_order) {
    case rpc_pb_SpiBitOrder_SPI_BIT_ORDER_LSB_FIRST:
      bit_order = pw::spi::BitOrder::kLsbFirst;
      break;
    case rpc_pb_SpiBitOrder_SPI_BIT_ORDER_MSB_FIRST:
      bit_order = pw::spi::BitOrder::kMsbFirst;
      break;
    default:
      return pw::Status::InvalidArgument();
  }

  if (config.frequency == 0U) {
    return pw::Status::InvalidArgument();
  }
  _initiator.SetFrequency(config.frequency);
  _initiator.SetTimeoutMs(bridge::config::SPI_TIMEOUT_MS);
  return _initiator.Configure(pw::spi::Config{
      .polarity = polarity,
      .phase = phase,
      .bits_per_word = pw::spi::BitsPerWord(8),
      .bit_order = bit_order,
  });
}

pw::Status SPIServiceClass::transfer(pw::span<uint8_t> buffer) {
  if (!_initiator.IsInitialized()) {
    return pw::Status::FailedPrecondition();
  }
  if (buffer.empty()) {
    return pw::OkStatus();
  }

  const pw::ConstByteSpan tx =
      pw::as_bytes(pw::span<const uint8_t>(buffer.data(), buffer.size()));
  const pw::ByteSpan rx =
      pw::as_writable_bytes(pw::span<uint8_t>(buffer.data(), buffer.size()));
  return _initiator.WriteRead(tx, rx);
}
SPIServiceType SPIService;
#endif
