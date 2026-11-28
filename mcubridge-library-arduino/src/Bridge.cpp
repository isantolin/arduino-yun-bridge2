#define PW_LOG_MODULE_NAME "BRG"

#include "Bridge.h"

#include <etl/algorithm.h>
#include <etl/fixed_iterator.h>
#include <etl/utility.h>
#include <pw_assert/assert.h>
#include <pw_bytes/endian.h>

#include "hal/hal.h"
#include "security/security.h"
#if defined(ARDUINO_ARCH_AVR)
#include "pw_assert_avr/assert_avr.h"
#endif
#include "services/Console.h"
#include "services/DataStore.h"
#if defined(ARDUINO_ARCH_AVR) || defined(__AVR__)
#include "services/EepromTransfer.h"
#endif
#include "services/FileSystem.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "services/SPIService.h"

namespace etl {
void __attribute__((weak)) handle_error(const etl::exception&) {
  Bridge.enterSafeState();
}
}  // namespace etl

namespace {

#if defined(ARDUINO_ARCH_AVR)
void BridgePanicHook(int) {
  // Assertions are fatal. Put all application outputs into their configured
  // safe state before the backend halts; never write raw bytes to the HDLC
  // UART.
  bridge::hal::forceSafeState();
}
#endif

bool isLinkSyncRequest(const pw::rpc::PacketMeta& meta) {
  return meta.destination_is_server() &&
         pw::rpc::internal::UnwrapServiceId(meta.service_id()) ==
             pw::tokenizer::Hash("rpc.pb.SystemService") &&
         pw::rpc::internal::UnwrapMethodId(meta.method_id()) ==
             pw::tokenizer::Hash("LinkSync");
}

bool isLinkResetRequest(const pw::rpc::PacketMeta& meta) {
  return meta.destination_is_server() &&
         pw::rpc::internal::UnwrapServiceId(meta.service_id()) ==
             pw::tokenizer::Hash("rpc.pb.SystemService") &&
         pw::rpc::internal::UnwrapMethodId(meta.method_id()) ==
             pw::tokenizer::Hash("LinkReset");
}

bool isMetricServiceRequest(const pw::rpc::PacketMeta& meta) {
  return pw::rpc::internal::UnwrapServiceId(meta.service_id()) ==
         pw::tokenizer::Hash("pw.metric.proto.MetricService");
}

PW_METRIC_GROUP_GLOBAL(g_bridge_metrics, "bridge");
PW_METRIC_STATIC(g_bridge_metrics, g_metric_tx_frames, "tx_frames", 0U);
PW_METRIC_STATIC(g_bridge_metrics, g_metric_rx_frames, "rx_frames", 0U);
PW_METRIC_STATIC(g_bridge_metrics, g_metric_safe_state_entries,
                 "safe_state_entries", 0U);
PW_METRIC_STATIC(g_bridge_metrics, g_metric_wcet_max_us, "wcet_max_us", 0U);
PW_METRIC_STATIC(g_bridge_metrics, g_metric_free_memory_bytes,
                 "free_memory_bytes", 0U);
PW_METRIC_STATIC(g_bridge_metrics, g_metric_stack_used_bytes,
                 "stack_used_bytes", 0U);

pw::metric::MetricService g_metric_service(pw::metric::global_metrics,
                                           pw::metric::global_groups);

constexpr uint32_t kMetricValueUnavailable = static_cast<uint32_t>(-1);

enum class PinRuntimeMode : uint8_t {
  kInput,
  kInputPullup,
  kOutputLow,
  kOutputHigh,
  kPwmLow,
  kPwmHigh,
};

etl::array<PinRuntimeMode, NUM_DIGITAL_PINS> g_pin_runtime_modes{};

bool isValidDigitalPin(uint32_t pin) {
  if (pin >= NUM_DIGITAL_PINS) return false;
#if defined(ARDUINO_ARCH_ESP32)
  return digitalPinIsValid(static_cast<uint8_t>(pin));
#else
  return true;
#endif
}

bool isValidDigitalOutputPin(uint32_t pin) {
  if (!isValidDigitalPin(pin)) return false;
#if defined(ARDUINO_ARCH_ESP32)
  return digitalPinCanOutput(static_cast<uint8_t>(pin));
#else
  return true;
#endif
}

void resetPinRuntimeModesToSafeState() {
  for (uint16_t index = 0; index < NUM_DIGITAL_PINS; ++index) {
    const uint8_t pin = static_cast<uint8_t>(index);
    if (pin == PIN_SPI_SS) {
      g_pin_runtime_modes[pin] = PinRuntimeMode::kOutputHigh;
    } else if (pin == PIN_SPI_SCK || pin == PIN_SPI_MOSI) {
      g_pin_runtime_modes[pin] = PinRuntimeMode::kOutputLow;
    } else if (pin == PIN_SPI_MISO) {
      g_pin_runtime_modes[pin] = PinRuntimeMode::kInput;
    } else if (pin == PIN_WIRE_SDA || pin == PIN_WIRE_SCL) {
      g_pin_runtime_modes[pin] = PinRuntimeMode::kInputPullup;
    } else if (pin < 2U) {
      // HAL deliberately leaves the supervisor UART pins unchanged.
      continue;
    } else if constexpr (bridge::config::SAFE_START_PINS_ENABLED) {
#if defined(ARDUINO_ARCH_ESP32)
      if (!digitalPinCanOutput(pin)) continue;
#endif
      g_pin_runtime_modes[pin] = PinRuntimeMode::kOutputLow;
    } else {
      g_pin_runtime_modes[pin] = PinRuntimeMode::kInputPullup;
    }
  }
}

void setPinRuntimeModeForSafety(uint8_t pin,
                                bridge::hal::SafetyPinState state) {
  if (!isValidDigitalPin(pin)) return;
#if defined(ARDUINO_ARCH_ESP32)
  if ((state == bridge::hal::SafetyPinState::SAFE_PIN_LOW ||
       state == bridge::hal::SafetyPinState::SAFE_PIN_HIGH) &&
      !isValidDigitalOutputPin(pin)) {
    return;
  }
#endif
  switch (state) {
    case bridge::hal::SafetyPinState::SAFE_PIN_LOW:
      g_pin_runtime_modes[pin] = PinRuntimeMode::kOutputLow;
      break;
    case bridge::hal::SafetyPinState::SAFE_PIN_HIGH:
      g_pin_runtime_modes[pin] = PinRuntimeMode::kOutputHigh;
      break;
    case bridge::hal::SafetyPinState::SAFE_PIN_INPUT_PULLUP:
      g_pin_runtime_modes[pin] = PinRuntimeMode::kInputPullup;
      break;
    case bridge::hal::SafetyPinState::SAFE_PIN_INPUT:
    default:
      g_pin_runtime_modes[pin] = PinRuntimeMode::kInput;
      break;
  }
}

void refreshMemoryMetrics() {
  g_metric_free_memory_bytes.Set(bridge::hal::getFreeMemory());
  const bridge::hal::StackWatermark watermark =
      bridge::hal::getStackWatermark();
  g_metric_stack_used_bytes.Set(watermark.budget_bytes == 0U
                                    ? kMetricValueUnavailable
                                    : watermark.used_bytes);
}

class GpioRpcService final
    : public rpc::pb::pw_rpc::nanopb::GpioService::Service<GpioRpcService> {
 public:
  pw::Status SetPinMode(const rpc_pb_PinMode& request, rpc_pb_Empty&) {
    return BridgeClass::_handleSetPinMode(request);
  }

  pw::Status DigitalWrite(const rpc_pb_DigitalWrite& request, rpc_pb_Empty&) {
    return BridgeClass::_handleDigitalWrite(request);
  }

  pw::Status AnalogWrite(const rpc_pb_AnalogWrite& request, rpc_pb_Empty&) {
    return BridgeClass::_handleAnalogWrite(request);
  }

  pw::Status DigitalRead(const rpc_pb_PinRead& request,
                         rpc_pb_DigitalReadResponse& response) {
    if (!isValidDigitalPin(request.pin)) {
      return pw::Status::InvalidArgument();
    }
    // Observation-only mode samples the physical pin without changing its
    // direction, pull-up configuration, or output latch.
    pw::digital_io::ArduinoDigitalInOut input(
        static_cast<uint8_t>(request.pin),
        false,
        pw::digital_io::State::kInactive,
        false);
    (void)input.Enable();
    auto value = input.GetState();
    response.value =
        (value.ok() && value.value() == pw::digital_io::State::kActive) ? 1U
                                                                        : 0U;
    return value.status();
  }

  pw::Status AnalogRead(const rpc_pb_PinRead& request,
                        rpc_pb_AnalogReadResponse& response) {
    if (!isValidDigitalPin(request.pin)) return pw::Status::InvalidArgument();
    pw::analog::ArduinoAnalogInput input(
        static_cast<uint8_t>(request.pin), bridge::config::ADC_MAX_SAMPLE);
    auto sample = input.TryReadUntil(pw::chrono::SystemClock::time_point::max());
    if (sample.ok()) {
      response.value = static_cast<uint32_t>(sample.value());
    }
    return sample.status();
  }

  void SubscribePins(
      const rpc_pb_PinSubscribeRequest& request,
      pw::rpc::NanopbServerWriter<rpc_pb_PinUpdateEvent>& writer) {
    Bridge._handlePinSubscribe(request, writer);
  }
};

class SystemRpcService final
    : public rpc::pb::pw_rpc::nanopb::SystemService::Service<SystemRpcService> {
 public:
  pw::Status GetVersion(const rpc_pb_SubscribeRequest&,
                        rpc_pb_VersionResponse& response) {
    BridgeClass::_fillVersion(response);
    return pw::OkStatus();
  }

  pw::Status GetFreeMemory(const rpc_pb_SubscribeRequest&,
                           rpc_pb_FreeMemoryResponse& response) {
    BridgeClass::_fillFreeMemory(response);
    return pw::OkStatus();
  }

  pw::Status LinkSync(const rpc_pb_LinkSync& request,
                      rpc_pb_LinkSync& response) {
    Bridge._handleLinkSync(request, response);
    return pw::OkStatus();
  }

  pw::Status LinkReset(const rpc_pb_SubscribeRequest&, rpc_pb_Empty&) {
    Bridge._handleLinkReset();
    return pw::OkStatus();
  }

  pw::Status GetCapabilities(const rpc_pb_SubscribeRequest&,
                             rpc_pb_Capabilities& response) {
    Bridge._fillCapabilities(response);
    return pw::OkStatus();
  }

  pw::Status SetBaudrate(const rpc_pb_SetBaudratePacket& request,
                         rpc_pb_SetBaudratePacket& response) {
    response = rpc_pb_SetBaudratePacket_init_default;
    Bridge._handleSetBaudrate(request);
    return pw::OkStatus();
  }

  pw::Status EnterBootloader(const rpc_pb_EnterBootloader& request,
                             rpc_pb_Empty&) {
    Bridge._handleEnterBootloader(request);
    return pw::OkStatus();
  }

  pw::Status Xoff(const rpc_pb_SubscribeRequest&, rpc_pb_Empty&) {
    return pw::OkStatus();
  }

  pw::Status Xon(const rpc_pb_SubscribeRequest&, rpc_pb_Empty&) {
    return pw::OkStatus();
  }
};

class ClockSyncRpcService final
    : public rpc::pb::pw_rpc::nanopb::ClockSyncService::Service<
          ClockSyncRpcService> {
 public:
  pw::Status ClockSync(const rpc_pb_ClockSyncRequest& request,
                       rpc_pb_ClockSyncResponse& response) {
    response.host_time_us = request.host_time_us;
    response.mcu_time_us = ::micros();
    return pw::OkStatus();
  }
};

#if BRIDGE_ENABLE_SPI
class SpiRpcService final
    : public rpc::pb::pw_rpc::nanopb::SpiService::Service<SpiRpcService> {
 public:
  pw::Status Begin(const rpc_pb_SubscribeRequest&, rpc_pb_Empty&) {
    SPIService.begin();
    return pw::OkStatus();
  }

  pw::Status End(const rpc_pb_SubscribeRequest&, rpc_pb_Empty&) {
    SPIService.end();
    return pw::OkStatus();
  }

  pw::Status SetConfig(const rpc_pb_SpiConfig& request, rpc_pb_Empty&) {
    return SPIService.setConfig(request);
  }

  pw::Status Transfer(const rpc_pb_SpiTransfer& request,
                      rpc_pb_SpiTransferResponse& response) {
    const size_t len = etl::min(static_cast<size_t>(request.data.size),
                                sizeof(response.data.bytes));
    etl::copy_n(request.data.bytes, len, response.data.bytes);
    const pw::Status status =
        SPIService.transfer(pw::span<uint8_t>(response.data.bytes, len));
    if (!status.ok()) return status;
    response.data.size = static_cast<pb_size_t>(len);
    return pw::OkStatus();
  }
};
#endif

#if BRIDGE_ENABLE_FILESYSTEM
class FileSystemRpcService final
    : public rpc::pb::pw_rpc::nanopb::FileSystemService::Service<
          FileSystemRpcService> {
 public:
  pw::Status Remove(const rpc_pb_FileRemove& request, rpc_pb_Empty&) {
    FileSystem._onRemove(request);
    return pw::OkStatus();
  }

  // Uploads only flow MCU -> MPU (FileSystem.upload()); the MCU never
  // serves them.
  pw::Status Upload(const rpc_pb_FileUpload&, rpc_pb_Empty&) {
    return pw::Status::Unimplemented();
  }
};
#endif

#if BRIDGE_ENABLE_MAILBOX
class MailboxRpcService final
    : public rpc::pb::pw_rpc::nanopb::MailboxService::Service<
          MailboxRpcService> {
 public:
  pw::Status Push(const rpc_pb_MailboxPush& request, rpc_pb_Empty&) {
    Mailbox._enqueue(
        pw::span<const uint8_t>(request.data.bytes, request.data.size));
    return pw::OkStatus();
  }

  pw::Status Read(const rpc_pb_SubscribeRequest&, rpc_pb_MailboxReadResponse&) {
    return pw::Status::NotFound();
  }

  pw::Status Available(const rpc_pb_SubscribeRequest&,
                       rpc_pb_MailboxAvailableResponse&) {
    return pw::Status::NotFound();
  }

  pw::Status Processed(const rpc_pb_MailboxProcessed&, rpc_pb_Empty&) {
    return pw::Status::NotFound();
  }
};
#endif

GpioRpcService g_gpio_service;
SystemRpcService g_system_service;
ClockSyncRpcService g_clock_sync_service;
#if BRIDGE_ENABLE_SPI
SpiRpcService g_spi_service;
#endif
#if BRIDGE_ENABLE_FILESYSTEM
FileSystemRpcService g_file_system_service;
#endif
#if BRIDGE_ENABLE_MAILBOX
MailboxRpcService g_mailbox_service;
#endif
BridgeClass* g_rpc_services_owner = nullptr;
BridgeClass* g_observers_owner = nullptr;

}  // namespace

void BridgeClass::_clearObservers() {
  _observers.clear();
  if (g_observers_owner == this) {
    g_observers_owner = nullptr;
  }
}

void BridgeClass::_notifyObservers(bridge::SystemEvent event) {
  for (bridge::BridgeObserver& observer : _observers) {
    observer.notification(event);
  }
}

void BridgeClass::_initObservers() {
  if (g_observers_owner != nullptr) {
    g_observers_owner->_clearObservers();
  }
  _observers.clear();
#if BRIDGE_ENABLE_SPI
  _observers.push_front(SPIService);
#endif
#if BRIDGE_ENABLE_PROCESS
  _observers.push_front(Process);
#endif
#if BRIDGE_ENABLE_MAILBOX
  _observers.push_front(Mailbox);
#endif
#if BRIDGE_ENABLE_DATASTORE
  _observers.push_front(DataStore);
#endif
#if BRIDGE_ENABLE_CONSOLE
  _observers.push_front(Console);
#endif
  g_observers_owner = this;
}

BridgeClass::BridgeClass(Stream& stream)
    : _stream(&stream),
      _arduino_stream(stream),
      _rpc_channel_output(*this),
      _rpc_channel(pw::rpc::Channel::Create<1>(&_rpc_channel_output)),
      _rpc_client(pw::span<pw::rpc::Channel>(&_rpc_channel, 1)),
      _rpc_server(pw::span<pw::rpc::Channel>(&_rpc_channel, 1)),
      _transfer_thread(pw::ByteSpan(_transfer_chunk_storage.data(),
                                    _transfer_chunk_storage.size()),
                       pw::ByteSpan(_transfer_encode_storage.data(),
                                    _transfer_encode_storage.size())),
      _transfer_service(_transfer_thread, 256),
      _transfer_client(_rpc_client, 1, _transfer_thread, 256) {
#if defined(ARDUINO_ARCH_AVR)
  pw_assert_avr_SetPanicHook(BridgePanicHook);
#endif
}

BridgeClass::BridgeClass(HardwareSerial& serial)
    : BridgeClass(static_cast<Stream&>(serial)) {
  _hardware_serial = &serial;
}

void BridgeClass::_unregisterRpcServices() {
  if (!_rpc_services_registered) return;
  PW_ASSERT(g_rpc_services_owner == this);
  _rpc_server.UnregisterService(g_gpio_service, g_system_service,
                                g_clock_sync_service
#if BRIDGE_ENABLE_SPI
                                ,
                                g_spi_service
#endif
#if BRIDGE_ENABLE_FILESYSTEM
                                ,
                                g_file_system_service
#endif
#if BRIDGE_ENABLE_MAILBOX
                                ,
                                g_mailbox_service
#endif
                                ,
                                _transfer_service, g_metric_service);
  _rpc_services_registered = false;
  g_rpc_services_owner = nullptr;
}

BridgeClass::~BridgeClass() {
  // Detach external stream references first so teardown never performs I/O on
  // a stream whose lifetime has already ended.
  pw_log_avr_SetLogSink(nullptr);
  _stream = nullptr;
  _hardware_serial = nullptr;
  _arduino_stream.setStream(static_cast<Stream*>(nullptr));
#if defined(ARDUINO_ARCH_AVR) || defined(__AVR__)
  bridge::services::unregisterEepromTransferHandler(_transfer_service);
#endif
#if BRIDGE_ENABLE_FILESYSTEM
  FileSystemClass::unregisterUploadHandler(_transfer_service);
#endif
  _pin_stream_writer = {};
  _clearObservers();
  _unregisterRpcServices();
}

pw::Status BridgeClass::RpcChannelOutput::Send(pw::ConstByteSpan packet) {
  const auto data = pw::span<const uint8_t>(
      reinterpret_cast<const uint8_t*>(packet.data()), packet.size());
  return bridge_._writeHdlcFrame(rpc::RPC_HDLC_ADDRESS_RPC, data,
                                 bridge_._rpc_plaintext_response)
             ? pw::OkStatus()
             : pw::Status::Unavailable();
}

void BridgeClass::setStream(Stream& stream) {
  // Generic streams (USB CDC, sockets, emulator shims) have no baud rate.
  _stream = &stream;
  _arduino_stream.setStream(stream);
  _hardware_serial = nullptr;
}

void BridgeClass::setStream(HardwareSerial& serial) {
  setStream(static_cast<Stream&>(serial));
  _hardware_serial = &serial;
}

void BridgeClass::_initializeRuntime() {
  g_pin_runtime_modes.fill(PinRuntimeMode::kInput);
  _initObservers();
  _timer_last_tick_ms = 0;
  _state_flags = 0U;
  _clearLinkSession();

  pw_log_avr_SetLogSink(
      [](uint8_t /*level*/, const uint8_t* log_data, size_t size) {
        (void)Bridge.sendLogStream(pw::span<const uint8_t>(log_data, size));
      });

  if (_rpc_services_registered) return;
  if (g_rpc_services_owner != nullptr) {
    g_rpc_services_owner->_unregisterRpcServices();
  }
  _rpc_server.RegisterService(g_gpio_service, g_system_service,
                              g_clock_sync_service
#if BRIDGE_ENABLE_SPI
                              ,
                              g_spi_service
#endif
#if BRIDGE_ENABLE_FILESYSTEM
                              ,
                              g_file_system_service
#endif
#if BRIDGE_ENABLE_MAILBOX
                              ,
                              g_mailbox_service
#endif
                              ,
                              _transfer_service, g_metric_service);
  _rpc_services_registered = true;
  g_rpc_services_owner = this;
}

void BridgeClass::begin(uint32_t baudrate, const char* secret) {
  _initializeRuntime();
  g_metric_tx_frames.Set(0U);
  g_metric_rx_frames.Set(0U);
  g_metric_safe_state_entries.Set(0U);
  g_metric_wcet_max_us.Set(0U);
  refreshMemoryMetrics();

  rpc::security::secure_zero(
      pw::span<uint8_t>(_shared_secret.data(), _shared_secret.size()));
  _shared_secret.clear();
  bool secret_too_long = false;
  if (secret != nullptr) {
    const etl::string_view s(secret);
    if (s.size() > _shared_secret.capacity()) {
      secret_too_long = true;
    } else {
      const auto data_ptr = reinterpret_cast<const uint8_t*>(s.data());
      _shared_secret.assign(data_ptr, data_ptr + s.size());
    }
  }
  bridge::hal::init();
  resetPinRuntimeModesToSafeState();
  if (!_fsm.is_started()) _fsm.start();
  _fsm.receive(bridge::fsm::EvReset());

  if (secret_too_long) {
    emitStatus(pw::Status::InvalidArgument());
    enterSafeState();
    return;
  }

#if defined(ARDUINO_ARCH_AVR) || defined(__AVR__)
  if (!bridge::services::registerEepromTransferHandler(_transfer_service,
                                                       _transfer_thread)) {
    emitStatus(pw::Status::Unavailable());
    enterSafeState();
    return;
  }
#endif
#if BRIDGE_ENABLE_FILESYSTEM
  if (!FileSystemClass::registerUploadHandler(_transfer_service,
                                              _transfer_thread)) {
    emitStatus(pw::Status::Unavailable());
    enterSafeState();
    return;
  }
#endif

  _setFlag(FLAG_POST_PASSED, bridge::hal::run_power_on_self_tests());
  if (!_hasFlag(FLAG_POST_PASSED)) {
    enterSafeState();
    return;
  }

  if (baudrate > 0 && _hardware_serial) _hardware_serial->begin(baudrate);
  _setFlag(FLAG_TX_ENABLED, true);
  _timers.clear();
  _timers.register_timer([]() { Bridge._onBaudrateChange(); },
                         bridge::config::BAUDRATE_CHANGE_DELAY_MS,
                         etl::timer::mode::SINGLE_SHOT);
  _timers.register_timer([]() { bridge::hal::enterBootloader(); },
                         bridge::config::BOOTLOADER_DELAY_MS,
                         etl::timer::mode::SINGLE_SHOT);
  _timers.register_timer([]() { Bridge._onHandshakeTimeout(); },
                         _response_timeout_ms, etl::timer::mode::SINGLE_SHOT);
  _timers.enable(true);
}

void BridgeClass::process() {
  const uint32_t start_us = ::micros();
  _watchdogTask();
  _serialTask();
  const pw::Status transfer_status = _transfer_thread.Poll(2);
  if (!transfer_status.ok() && transfer_status != pw::Status::Unavailable()) {
    emitStatus(transfer_status);
  }
  _timerTask();
  _subscriptionTask();
#if BRIDGE_ENABLE_CONSOLE
  Console.process();
#endif
#if BRIDGE_ENABLE_MAILBOX
  Mailbox.process();
#endif
  const uint32_t elapsed_us = ::micros() - start_us;
  _wcet_max_micros = etl::max(_wcet_max_micros, elapsed_us);
  g_metric_wcet_max_us.Set(_wcet_max_micros);
}

void BridgeClass::resetWcetStats() {
  _wcet_max_micros = 0;
  g_metric_wcet_max_us.Set(0U);
}

void BridgeClass::_watchdogTask() {
  bridge::hal::watchdog_kick();
  if (_watchdog_timeout_ms > 0 && isSynchronized()) {
    if (bridge::hal::nowMillis() - _last_rx_ms > _watchdog_timeout_ms) {
      enterSafeState();
    }
  }
}

void BridgeClass::_serialTask() {
  if (!_stream) return;
  // Drain up to RX_BUFFER_SIZE bytes per pass, but through a small stack
  // chunk: a complete frame is dispatched (RPC handler + AEAD response) while
  // this buffer is live, and on the ATmega2560 a full RX_BUFFER_SIZE copy here
  // pushed that call chain past the stack into .bss.
  constexpr size_t kReadChunkSize = 32U;
  static_assert(bridge::config::RX_BUFFER_SIZE % kReadChunkSize == 0U);
  constexpr size_t kReadPasses =
      bridge::config::RX_BUFFER_SIZE / kReadChunkSize;
  etl::array<pw::ByteSpan::element_type, kReadChunkSize> chunk;
  const etl::array<uint8_t, kReadPasses> passes{};
  // all_of stops at the first pass that finds no data.
  (void)etl::all_of(passes.begin(), passes.end(), [this, &chunk](uint8_t) {
    const auto read_result =
        _arduino_stream.Read(pw::ByteSpan(chunk.data(), chunk.size()));
    if (!read_result.ok())
      return false;  // no data: DoRead never returns 0 bytes OK
    const pw::ConstByteSpan rx_span = read_result.value();
    etl::for_each(rx_span.begin(), rx_span.end(),
                  [this](const pw::ByteSpan::element_type b) {
                    auto result = _hdlc_decoder.Process(b);
                    if (result.ok()) {
                      _handleReceivedHdlcFrame(result.value());
                    }
                  });
    return true;
  });

  const int avail = _stream->available();
  if (!_hasFlag(FLAG_SERIAL_XOFF) &&
      avail > bridge::config::FLOW_CONTROL_XOFF_THRESHOLD) {
    _setFlag(FLAG_SERIAL_XOFF, true);
    _sendFlowControl(true);
  } else if (_hasFlag(FLAG_SERIAL_XOFF) &&
             avail < bridge::config::FLOW_CONTROL_XON_THRESHOLD) {
    _setFlag(FLAG_SERIAL_XOFF, false);
    _sendFlowControl(false);
  }
}

namespace {
// Received plaintext lives in the HDLC decoder buffer (decrypted in place).
void zeroReceivedPlaintext(pw::span<const uint8_t> plaintext) {
  // NOLINTNEXTLINE(cppcoreguidelines-pro-type-const-cast)
  rpc::security::secure_zero(pw::span<uint8_t>(
      const_cast<uint8_t*>(plaintext.data()), plaintext.size()));
}
}  // namespace

void BridgeClass::_handleReceivedHdlcFrame(const pw::hdlc::Frame& frame) {
  _last_rx_ms = bridge::hal::nowMillis();
  g_metric_rx_frames.Increment();
  const uint64_t addr = frame.address();
  const auto raw_data = pw::span<const uint8_t>(
      reinterpret_cast<const uint8_t*>(frame.data().data()),
      frame.data().size());
  pw::span<const uint8_t> data = raw_data;
  pw::Result<pw::rpc::PacketMeta> meta_result = pw::Status::DataLoss();
  bool decrypted = false;

  if (addr == rpc::RPC_HDLC_ADDRESS_RPC) {
    const auto raw_meta = pw::rpc::PacketMeta::FromBuffer(frame.data());
    const bool clear_link_reset =
        raw_meta.ok() && isLinkResetRequest(raw_meta.value());

    if (!_shared_secret.empty() && _session_keys_ready && !clear_link_reset) {
      if (!_decryptHdlcFrame(addr, raw_data, data)) {
        emitStatus(pw::Status::Unauthenticated());
        return;
      }
      decrypted = true;
    } else if (!_shared_secret.empty() && !_session_keys_ready) {
      if (!raw_meta.ok() ||
          (!isLinkSyncRequest(raw_meta.value()) && !clear_link_reset)) {
        emitStatus(pw::Status::Unauthenticated());
        enterSafeState();
        return;
      }
    }

    if (!decrypted) {
      meta_result = raw_meta;
    }
  } else if (!_shared_secret.empty()) {
    if (!_session_keys_ready || !_decryptHdlcFrame(addr, raw_data, data)) {
      emitStatus(pw::Status::Unauthenticated());
      return;
    }
    decrypted = true;
  }

  if (addr == rpc::RPC_HDLC_ADDRESS_RPC) {
    if (decrypted) {
      meta_result = pw::rpc::PacketMeta::FromBuffer(pw::as_bytes(data));
    }
    if (!meta_result.ok()) {
      if (decrypted) {
        zeroReceivedPlaintext(data);
      }
      emitStatus(pw::Status::InvalidArgument());
      return;
    }
    const auto& meta = meta_result.value();
    if (meta.destination_is_server()) {
      if (!_shared_secret.empty() && _session_keys_ready &&
          isLinkSyncRequest(meta)) {
        emitStatus(pw::Status::FailedPrecondition());
        PW_DASSERT(decrypted);
        zeroReceivedPlaintext(data);
        return;
      }
      if (!_shared_secret.empty() && !isSynchronized() &&
          !isLinkSyncRequest(meta) && !isLinkResetRequest(meta)) {
        emitStatus(pw::Status::FailedPrecondition());
        enterSafeState();
        PW_DASSERT(decrypted);
        zeroReceivedPlaintext(data);
        return;
      }
      if (isMetricServiceRequest(meta)) {
        refreshMemoryMetrics();
      }
      _rpc_plaintext_response = isLinkSyncRequest(meta);
      const auto status = _rpc_server.ProcessPacket(pw::as_bytes(data));
      _rpc_plaintext_response = false;
      if (!status.ok()) emitStatus(status);
      _emitStackWatermarkLog();
    } else {
      (void)_rpc_client.ProcessPacket(pw::as_bytes(data));
      _emitStackWatermarkLog();
    }
  }
#if BRIDGE_ENABLE_CONSOLE
  else if (addr == rpc::RPC_HDLC_ADDRESS_CONSOLE) {
    Console.pushBytes(data.data(), data.size());
  }
#endif
  if (decrypted) {
    zeroReceivedPlaintext(data);
  }
}

bool BridgeClass::_writeHdlcFrame(uint64_t address,
                                  pw::span<const uint8_t> data,
                                  bool plaintext) {
  if (!_stream) return false;
  const bool ok =
      (plaintext || _shared_secret.empty())
          ? pw::hdlc::WriteUIFrame(address, pw::as_bytes(data), _arduino_stream)
                .ok()
          : _writeEncryptedHdlcFrame(address, data);
  if (ok) {
    g_metric_tx_frames.Increment();
  }
  return ok;
}

bool BridgeClass::_writeEncryptedHdlcFrame(uint64_t address,
                                           pw::span<const uint8_t> data) {
  constexpr size_t kAeadOverhead =
      rpc::RPC_AEAD_COUNTER_SIZE + rpc::RPC_AEAD_TAG_SIZE;
  // Same bound as the receive path, so every frame fits the peer's buffer.
  if (!_session_keys_ready || address > UINT8_MAX ||
      data.size() > bridge::config::RX_BUFFER_SIZE - kAeadOverhead ||
      _tx_aead_counter == UINT64_MAX) {
    return false;
  }

  static_assert(rpc::RPC_AEAD_NONCE_SIZE == 4U + rpc::RPC_AEAD_COUNTER_SIZE);
  static_assert(rpc::RPC_AEAD_COUNTER_SIZE == sizeof(uint64_t));
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> nonce = {};
  etl::array<uint8_t, 1U + rpc::RPC_AEAD_COUNTER_SIZE> aad = {};
  const uint64_t counter = ++_tx_aead_counter;
  aad[0] = static_cast<uint8_t>(address);
  pw::bytes::CopyInOrder(pw::endian::big, counter, nonce.data() + 4U);
  etl::copy_n(nonce.data() + 4U, rpc::RPC_AEAD_COUNTER_SIZE, aad.data() + 1U);

  // Stream counter || ciphertext || tag straight into the HDLC encoder. The
  // bytes on the wire are identical to encrypting into a frame-sized envelope
  // first, but the stack no longer holds a RX_BUFFER_SIZE copy, which was the
  // deepest consumer on the ATmega2560's response path.
  pw::hdlc::Encoder encoder(_arduino_stream);
  auto write_chunk = [&encoder](pw::span<const uint8_t> chunk) {
    return encoder.WriteData(pw::as_bytes(chunk)).ok();
  };
  bool ok = encoder.StartUnnumberedFrame(address).ok() &&
            write_chunk(pw::span<const uint8_t>(aad.data() + 1U,
                                                rpc::RPC_AEAD_COUNTER_SIZE));
  ok = ok && rpc::security::aead_encrypt_chunked(
                 pw::span<const uint8_t>(
                     _session_aead_keys.data() + rpc::RPC_AEAD_KEY_SIZE,
                     rpc::RPC_AEAD_KEY_SIZE),
                 pw::span<const uint8_t>(nonce), pw::span<const uint8_t>(aad),
                 data, rpc::security::AeadOutputSink(write_chunk));
  ok = ok && encoder.FinishFrame().ok();
  rpc::security::secure_zero(pw::span<uint8_t>(nonce));
  rpc::security::secure_zero(pw::span<uint8_t>(aad));
  return ok;
}

bool BridgeClass::_decryptHdlcFrame(uint64_t address,
                                    pw::span<const uint8_t> envelope,
                                    pw::span<const uint8_t>& plaintext) {
  plaintext = {};
  const size_t overhead = rpc::RPC_AEAD_COUNTER_SIZE + rpc::RPC_AEAD_TAG_SIZE;
  if (!_session_keys_ready || address > UINT8_MAX ||
      envelope.size() < overhead) {
    return false;
  }

  static_assert(rpc::RPC_AEAD_NONCE_SIZE == 4U + rpc::RPC_AEAD_COUNTER_SIZE);
  static_assert(rpc::RPC_AEAD_COUNTER_SIZE == sizeof(uint64_t));
  const uint64_t counter =
      pw::bytes::ReadInOrder<uint64_t>(pw::endian::big, envelope.data());
  if (counter == 0 || counter <= _rx_aead_counter) {
    return false;
  }

  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> nonce = {};
  etl::array<uint8_t, 1U + rpc::RPC_AEAD_COUNTER_SIZE> aad = {};
  aad[0] = static_cast<uint8_t>(address);
  etl::copy_n(envelope.data(), rpc::RPC_AEAD_COUNTER_SIZE, nonce.data() + 4U);
  etl::copy_n(envelope.data(), rpc::RPC_AEAD_COUNTER_SIZE, aad.data() + 1U);

  const size_t ciphertext_size = envelope.size() - overhead;
  const uint8_t* ciphertext = envelope.data() + rpc::RPC_AEAD_COUNTER_SIZE;
  const uint8_t* tag = ciphertext + ciphertext_size;
  // Decrypt in place inside the HDLC decoder buffer, which this object owns
  // and which is not reused until the frame handler returns. wolfCrypt
  // authenticates the ciphertext before the ChaCha20 pass overwrites it, so
  // a dedicated RX_BUFFER_SIZE plaintext copy is not needed on the 8 KiB AVR.
  // NOLINTNEXTLINE(cppcoreguidelines-pro-type-const-cast)
  uint8_t* const in_place = const_cast<uint8_t*>(ciphertext);
  const bool decrypted = rpc::security::aead_decrypt(
      pw::span<const uint8_t>(_session_aead_keys.data(),
                              rpc::RPC_AEAD_KEY_SIZE),
      pw::span<const uint8_t>(nonce), pw::span<const uint8_t>(aad),
      pw::span<const uint8_t>(ciphertext, ciphertext_size),
      pw::span<const uint8_t>(tag, rpc::RPC_AEAD_TAG_SIZE),
      pw::span<uint8_t>(in_place, ciphertext_size));
  rpc::security::secure_zero(pw::span<uint8_t>(nonce));
  rpc::security::secure_zero(pw::span<uint8_t>(aad));
  if (!decrypted) {
    return false;
  }

  _rx_aead_counter = counter;
  plaintext = pw::span<const uint8_t>(in_place, ciphertext_size);
  return true;
}

void BridgeClass::_clearLinkSession() {
#if BRIDGE_ENABLE_FILESYSTEM
  FileSystemClass::_resetUpload();
#endif
  _pin_stream_writer = {};
  rpc::security::secure_zero(pw::span<uint8_t>(_session_aead_keys));
  _tx_aead_counter = 0;
  _rx_aead_counter = 0;
  _session_keys_ready = false;
  _last_reported_stack_used = 0;
}

void BridgeClass::_emitStackWatermarkLog() {
#if defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_ESP32)
  if (!isSynchronized() || !isRpcTxReady()) return;
  const auto wm = bridge::hal::getStackWatermark();
  if (wm.budget_bytes == 0U || wm.used_bytes == _last_reported_stack_used) {
    return;
  }
  _last_reported_stack_used = wm.used_bytes;
  g_metric_stack_used_bytes.Set(wm.used_bytes);
#if defined(ARDUINO_ARCH_SAMD)
  PW_LOG_INFO("MCU_STACK_WATERMARK arch=SAMD21 used=%u budget=%u",
              static_cast<unsigned>(wm.used_bytes),
              static_cast<unsigned>(wm.budget_bytes));
#else
  PW_LOG_INFO("MCU_STACK_WATERMARK arch=ESP32 used=%u budget=%u",
              static_cast<unsigned>(wm.used_bytes),
              static_cast<unsigned>(wm.budget_bytes));
#endif
#endif
}

void BridgeClass::_timerTask() {
  const uint32_t now = bridge::hal::nowMillis();
  if (_timer_last_tick_ms == 0) _timer_last_tick_ms = now;
  const uint32_t elapsed = now - _timer_last_tick_ms;
  if (elapsed > 0) {
    _timers.tick(elapsed);
    _timer_last_tick_ms = now;
  }
}

bool BridgeClass::isSynchronized() const { return _fsm.isSynchronized(); }

bool BridgeClass::awaitSynchronization(uint32_t timeout_ms,
                                       uint32_t poll_delay_ms) {
  const uint32_t start_ms = bridge::hal::nowMillis();
  const uint32_t step_ms = etl::max<uint32_t>(poll_delay_ms, 1U);
  uint8_t cursor = 0U;
  uint8_t sentinel = 0U;
  (void)etl::find_if(etl::fixed_iterator<uint8_t*>(&cursor),
                     etl::fixed_iterator<uint8_t*>(&sentinel),
                     [this, start_ms, timeout_ms, step_ms](uint8_t) {
                       process();
                       if (isSynchronized()) return true;
                       if (bridge::hal::nowMillis() - start_ms >= timeout_ms) {
                         return true;
                       }
                       ::delay(step_ms);
                       return false;
                     });
  if (isSynchronized()) return true;
  enterSafeState();
  return false;
}

void BridgeClass::enterSafeState() {
  g_metric_safe_state_entries.Increment();
  if (isSynchronized()) {
    PW_LOG_WARN("BRIDGE_SAFE_STATE");
  }
  _clearLinkSession();
  bridge::hal::forceSafeState();
  resetPinRuntimeModesToSafeState();
  etl::for_each(_safety_pins.begin(), _safety_pins.end(),
                [](const SafetyPinEntry& entry) {
                  bridge::hal::applySafetyPin(entry.pin, entry.state);
                  setPinRuntimeModeForSafety(entry.pin, entry.state);
                });
  _setFlag(FLAG_TX_ENABLED, false);
  _setFlag(FLAG_SERIAL_XOFF, false);
  _fsm.receive(bridge::fsm::EvReset());
  _notifyObservers(bridge::SystemEvent::SAFE_STATE_ENTERED);
}

void BridgeClass::registerSafetyPin(uint8_t pin, SafetyPinState state) {
  auto it =
      etl::find_if(_safety_pins.begin(), _safety_pins.end(),
                   [pin](const SafetyPinEntry& e) { return e.pin == pin; });
  if (it != _safety_pins.end()) {
    it->state = state;
  } else if (!_safety_pins.full()) {
    _safety_pins.push_back({pin, state});
  }
}

void BridgeClass::clearSafetyPins() { _safety_pins.clear(); }

bool BridgeClass::sendConsoleStream(pw::span<const uint8_t> data) {
  if (!_stream || !_hasFlag(FLAG_TX_ENABLED)) return false;
  return _writeHdlcFrame(rpc::RPC_HDLC_ADDRESS_CONSOLE, data);
}

bool BridgeClass::sendLogStream(pw::span<const uint8_t> data) {
  if (!_stream || !_hasFlag(FLAG_TX_ENABLED)) return false;
  return _writeHdlcFrame(rpc::RPC_HDLC_ADDRESS_LOG, data);
}

void BridgeClass::_sendFlowControl(bool xoff) {
  if (!isRpcTxReady()) return;
  if (xoff) {
    PW_LOG_WARN("BRIDGE_FLOW_XOFF");
  } else {
    PW_LOG_INFO("BRIDGE_FLOW_XON");
  }
  rpc_pb_SubscribeRequest req = rpc_pb_SubscribeRequest_init_default;
  if (xoff) {
    (void)systemClient().Xoff(req);
  } else {
    (void)systemClient().Xon(req);
  }
}

void BridgeClass::_onHandshakeTimeout() {
  if (_fsm.isSynchronized()) return;
  _fsm.receive(bridge::fsm::EvTimeout());
  _setFlag(FLAG_TX_ENABLED, false);
}

void BridgeClass::_onBaudrateChange() {
  if (_pending_baudrate > 0) {
    if (_hardware_serial) _hardware_serial->begin(_pending_baudrate);
    _pending_baudrate = 0;
    _fsm.receive(bridge::fsm::EvBaudrateApplied());
  }
}

void BridgeClass::_handleSetBaudrate(const rpc_pb_SetBaudratePacket& msg) {
  if (msg.baudrate == 0 || msg.baudrate == _pending_baudrate) return;
  _pending_baudrate = msg.baudrate;
  _fsm.receive(bridge::fsm::EvBaudrateChange());
  _timers.start(bridge::scheduler::TIMER_BAUDRATE_CHANGE);
}

void BridgeClass::_handleEnterBootloader(const rpc_pb_EnterBootloader& msg) {
  if (msg.magic == rpc::RPC_BOOTLOADER_MAGIC) {
    _fsm.receive(bridge::fsm::EvEnterBootloader());
    _timers.start(bridge::scheduler::TIMER_BOOTLOADER_DELAY);
  }
}

pw::Status BridgeClass::_handleSetPinMode(const rpc_pb_PinMode& m) {
  if (!isValidDigitalPin(m.pin)) return pw::Status::InvalidArgument();
  const uint8_t pin = static_cast<uint8_t>(m.pin);
  switch (m.mode) {
    case rpc_pb_PinModeType_PIN_INPUT: {
      pw::digital_io::ArduinoDigitalIn input(pin);
      g_pin_runtime_modes[pin] = PinRuntimeMode::kInput;
      return input.Enable();
    }
    case rpc_pb_PinModeType_PIN_INPUT_PULLUP: {
      pw::digital_io::ArduinoDigitalIn input(pin, true);
      g_pin_runtime_modes[pin] = PinRuntimeMode::kInputPullup;
      return input.Enable();
    }
    case rpc_pb_PinModeType_PIN_OUTPUT: {
#if defined(ARDUINO_ARCH_ESP32)
      if (!isValidDigitalOutputPin(m.pin)) return pw::Status::InvalidArgument();
#endif
      const PinRuntimeMode previous_mode = g_pin_runtime_modes[pin];
      const bool initial_active =
          previous_mode == PinRuntimeMode::kInputPullup ||
          previous_mode == PinRuntimeMode::kOutputHigh ||
          previous_mode == PinRuntimeMode::kPwmHigh;
      pw::digital_io::ArduinoDigitalOut output(
          pin, initial_active ? pw::digital_io::State::kActive
                              : pw::digital_io::State::kInactive);
      g_pin_runtime_modes[pin] = initial_active ? PinRuntimeMode::kOutputHigh
                                                : PinRuntimeMode::kOutputLow;
      return output.Enable();
    }
    default:
      return pw::Status::InvalidArgument();
  }
}

pw::Status BridgeClass::_handleDigitalWrite(const rpc_pb_DigitalWrite& m) {
  if (!isValidDigitalOutputPin(m.pin)) return pw::Status::InvalidArgument();
  const uint8_t pin = static_cast<uint8_t>(m.pin);
  const bool active = m.value != 0U;
  pw::digital_io::ArduinoDigitalOut output(
      pin, active ? pw::digital_io::State::kActive
                  : pw::digital_io::State::kInactive);
  g_pin_runtime_modes[pin] =
      active ? PinRuntimeMode::kOutputHigh : PinRuntimeMode::kOutputLow;
  return output.Enable();
}

pw::Status BridgeClass::_handleAnalogWrite(const rpc_pb_AnalogWrite& m) {
  if (!isValidDigitalOutputPin(m.pin)) return pw::Status::InvalidArgument();
  const uint8_t pin = static_cast<uint8_t>(m.pin);
  const uint8_t duty = static_cast<uint8_t>(
      etl::clamp<uint32_t>(m.value, 0UL, 255UL));
  // Pigweed pw_analog models ADC inputs only; Arduino PWM remains the
  // platform-owned output peripheral until an upstream PWM API exists.
  ::analogWrite(pin, static_cast<int>(duty));
  g_pin_runtime_modes[pin] =
      duty == 0U ? PinRuntimeMode::kPwmLow : PinRuntimeMode::kPwmHigh;
  return pw::OkStatus();
}

void BridgeClass::_handlePinSubscribe(
    const rpc_pb_PinSubscribeRequest& m,
    pw::rpc::NanopbServerWriter<rpc_pb_PinUpdateEvent>& writer) {
  if (m.enabled && !isValidDigitalPin(m.pin)) {
    (void)writer.Finish(pw::Status::InvalidArgument());
    return;
  }
  if (m.enabled && m.mode != rpc_pb_PinModeType_PIN_INPUT &&
      m.mode != rpc_pb_PinModeType_PIN_INPUT_PULLUP &&
      m.mode != rpc_pb_PinModeType_PIN_OUTPUT) {
    (void)writer.Finish(pw::Status::InvalidArgument());
    return;
  }

  auto it = etl::find_if(
      _pin_subscriptions.begin(), _pin_subscriptions.end(),
      [pin = m.pin](const PinSubscription& sub) { return sub.pin == pin; });
  if (!m.enabled) {
    if (it != _pin_subscriptions.end()) _pin_subscriptions.erase(it);
  } else {
    const bool is_new = it == _pin_subscriptions.end();
    if (is_new) {
      if (_pin_subscriptions.full()) {
        (void)writer.Finish(pw::Status::ResourceExhausted());
        return;
      }
      _pin_subscriptions.push_back(
          {static_cast<uint8_t>(m.pin), 0, 0, 0, 0, 0, false});
      it = _pin_subscriptions.end() - 1;
    }

    const uint8_t pin = static_cast<uint8_t>(m.pin);
    if (m.mode == rpc_pb_PinModeType_PIN_INPUT ||
        m.mode == rpc_pb_PinModeType_PIN_INPUT_PULLUP) {
      pw::digital_io::ArduinoDigitalIn input(
          pin, m.mode == rpc_pb_PinModeType_PIN_INPUT_PULLUP);
      (void)input.Enable();
      g_pin_runtime_modes[pin] =
          m.mode == rpc_pb_PinModeType_PIN_INPUT_PULLUP
              ? PinRuntimeMode::kInputPullup
              : PinRuntimeMode::kInput;
    }
    it->mode = static_cast<uint8_t>(m.mode);
    it->interval_ms = static_cast<uint16_t>(m.interval_ms);
    it->hysteresis = static_cast<uint16_t>(m.hysteresis);
    it->active = true;
    it->last_report_ms = 0;
  }

  _pin_stream_writer = etl::move(writer);
  const bool any_active =
      etl::any_of(_pin_subscriptions.begin(), _pin_subscriptions.end(),
                  [](const PinSubscription& sub) { return sub.active; });
  if (!any_active) {
    (void)_pin_stream_writer.Finish(pw::OkStatus());
  }
}

void BridgeClass::_handleLinkSync(const rpc_pb_LinkSync& request,
                                  rpc_pb_LinkSync& response) {
  response = rpc_pb_LinkSync_init_default;
  const bool nonce_valid =
      request.nonce.size == rpc::RPC_HANDSHAKE_NONCE_LENGTH;
  const bool tag_shape_valid =
      _shared_secret.empty()
          ? request.tag.size == 0U
          : request.tag.size == rpc::RPC_HANDSHAKE_TAG_LENGTH;
  if (!nonce_valid || !tag_shape_valid) {
    _clearLinkSession();
    _fsm.receive(bridge::fsm::EvHandshakeFailed());
    emitStatus(pw::Status::InvalidArgument());
    return;
  }

  bridge::pb::copy_bytes(response.nonce, request.nonce.bytes,
                         rpc::RPC_HANDSHAKE_NONCE_LENGTH);
  if (!_shared_secret.empty()) {
    if (!rpc::security::handshake_verify_tag(
            pw::span<const uint8_t>(_shared_secret),
            pw::span<const uint8_t>(request.nonce.bytes, request.nonce.size),
            pw::span<const uint8_t>(request.tag.bytes, request.tag.size))) {
      _clearLinkSession();
      _fsm.receive(bridge::fsm::EvHandshakeFailed());
      emitStatus(pw::Status::Unauthenticated());
      return;
    }

    // Handshake v2 (ADR-0005): the MCU contributes a never-repeating nonce.
    // The response tag and the session-key salt cover nonce || mcu_nonce, so
    // replaying a recorded host LinkSync yields fresh, different keys.
    const auto host_nonce =
        pw::span<const uint8_t>(request.nonce.bytes, request.nonce.size);
    const auto mcu_nonce = pw::span<uint8_t>(response.mcu_nonce.bytes,
                                             rpc::RPC_HANDSHAKE_NONCE_LENGTH);
    const bool mcu_nonce_ok = bridge::hal::fillLinkNonce(
        pw::span<const uint8_t>(_shared_secret), mcu_nonce);
    const bool response_tag_ok =
        mcu_nonce_ok &&
        rpc::security::handshake_generate_response_tag(
            pw::span<const uint8_t>(_shared_secret), host_nonce,
            pw::span<const uint8_t>(mcu_nonce),
            pw::span<uint8_t>(response.tag.bytes, sizeof(response.tag.bytes)));
    bool session_keys_ok =
        response_tag_ok && rpc::security::derive_session_keys(
                               pw::span<const uint8_t>(_shared_secret),
                               host_nonce, pw::span<const uint8_t>(mcu_nonce),
                               pw::span<uint8_t>(_session_aead_keys));
    if (!session_keys_ok) {
      _clearLinkSession();
      _fsm.receive(bridge::fsm::EvHandshakeFailed());
      emitStatus(pw::Status::Internal());
      return;
    }
    response.tag.size = rpc::RPC_HANDSHAKE_TAG_LENGTH;
    response.mcu_nonce.size = rpc::RPC_HANDSHAKE_NONCE_LENGTH;
    _tx_aead_counter = 0;
    _rx_aead_counter = 0;
    _session_keys_ready = true;
  } else {
    _clearLinkSession();
  }
  _fsm.receive(bridge::fsm::EvHandshakeStart());
  _fsm.receive(bridge::fsm::EvHandshakeComplete());
  _setFlag(FLAG_TX_ENABLED, true);
  _timers.stop(bridge::scheduler::TIMER_HANDSHAKE_TIMEOUT);
}

void BridgeClass::_handleLinkReset() {
  _clearLinkSession();
  _setFlag(FLAG_TX_ENABLED, true);
  _fsm.receive(bridge::fsm::EvReset());
  _notifyObservers(bridge::SystemEvent::UNSYNCHRONIZED);
  _timers.stop(bridge::scheduler::TIMER_HANDSHAKE_TIMEOUT);
  _timers.set_period(bridge::scheduler::TIMER_HANDSHAKE_TIMEOUT,
                     _response_timeout_ms);
  _timers.start(bridge::scheduler::TIMER_HANDSHAKE_TIMEOUT);
}

void BridgeClass::_subscriptionTask() {
  if (!isSynchronized()) return;
  const uint32_t now = bridge::hal::nowMillis();
  etl::for_each(_pin_subscriptions.begin(), _pin_subscriptions.end(),
                [this, now](PinSubscription& sub) {
                  if (!sub.active) return;
                  if (now - sub.last_report_ms >= sub.interval_ms) {
                    uint16_t val = 0U;
                    if (sub.mode == rpc_pb_PinModeType_PIN_INPUT ||
                        sub.mode == rpc_pb_PinModeType_PIN_INPUT_PULLUP) {
                      pw::digital_io::ArduinoDigitalIn input(
                          sub.pin,
                          sub.mode == rpc_pb_PinModeType_PIN_INPUT_PULLUP,
                          false);
                      (void)input.Enable();
                      auto state = input.GetState();
                      val = (state.ok() &&
                             state.value() == pw::digital_io::State::kActive)
                                ? 1U
                                : 0U;
                    } else {
                      pw::analog::ArduinoAnalogInput input(
                          sub.pin, bridge::config::ADC_MAX_SAMPLE);
                      auto sample = input.TryReadUntil(
                          pw::chrono::SystemClock::time_point::max());
                      val = sample.ok() ? static_cast<uint16_t>(sample.value())
                                        : 0U;
                    }
                    const uint16_t diff =
                        (val > sub.last_value)
                            ? static_cast<uint16_t>(val - sub.last_value)
                            : static_cast<uint16_t>(sub.last_value - val);
                    if (diff >= sub.hysteresis || sub.last_report_ms == 0) {
                      sub.last_value = val;
                      sub.last_report_ms = now;
                      (void)sendPinEvent(sub.pin, val);
                    }
                  }
                });
}

bool BridgeClass::sendPinEvent(uint8_t pin, uint16_t value) {
  if (!_stream || !_hasFlag(FLAG_TX_ENABLED) || !isSynchronized() ||
      !_pin_stream_writer.active()) {
    return false;
  }
  rpc_pb_PinUpdateEvent evt = rpc_pb_PinUpdateEvent_init_default;
  evt.pin = pin;
  evt.value = value;
  evt.timestamp_micros = ::micros();
  return _pin_stream_writer.Write(evt).ok();
}

void BridgeClass::_fillVersion(rpc_pb_VersionResponse& resp) {
  resp.major = rpc::FIRMWARE_VERSION_MAJOR;
  resp.minor = rpc::FIRMWARE_VERSION_MINOR;
  resp.patch = static_cast<uint32_t>(rpc::FIRMWARE_VERSION_PATCH);
}

void BridgeClass::_fillFreeMemory(rpc_pb_FreeMemoryResponse& resp) {
  resp.value = static_cast<uint32_t>(bridge::hal::getFreeMemory());
}

void BridgeClass::_fillCapabilities(rpc_pb_Capabilities& resp) {
  bridge::hal::fillCapabilities(resp);
}
