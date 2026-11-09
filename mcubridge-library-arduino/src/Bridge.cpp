#include "Bridge.h"

#include <etl/algorithm.h>
#include <etl/fixed_iterator.h>
#include <etl/functional.h>
#include <etl/iterator.h>
#include <etl/utility.h>

#include "hal/ArchTraits.h"
#include "security/security.h"
#include "services/Console.h"
#include "services/DataStore.h"
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

uint32_t NowMillis() {
  return static_cast<uint32_t>(
      pw::chrono::SystemClock::now().time_since_epoch().count());
}

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

class GpioRpcService final
    : public rpc::pb::pw_rpc::nanopb::GpioService::Service<GpioRpcService> {
 public:
  pw::Status SetPinMode(const rpc_pb_PinMode& request, rpc_pb_Empty&) {
    BridgeClass::_handleSetPinMode(request);
    return pw::OkStatus();
  }

  pw::Status DigitalWrite(const rpc_pb_DigitalWrite& request, rpc_pb_Empty&) {
    BridgeClass::_handlePinWrite<rpc_pb_DigitalWrite, false>(request);
    return pw::OkStatus();
  }

  pw::Status AnalogWrite(const rpc_pb_AnalogWrite& request, rpc_pb_Empty&) {
    BridgeClass::_handlePinWrite<rpc_pb_AnalogWrite, true>(request);
    return pw::OkStatus();
  }

  pw::Status DigitalRead(const rpc_pb_PinRead& request,
                         rpc_pb_DigitalReadResponse& response) {
    response.value = digitalRead(request.pin);
    return pw::OkStatus();
  }

  pw::Status AnalogRead(const rpc_pb_PinRead& request,
                        rpc_pb_AnalogReadResponse& response) {
    response.value = analogRead(request.pin);
    return pw::OkStatus();
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
    SPIService.setConfig(request);
    return pw::OkStatus();
  }

  pw::Status Transfer(const rpc_pb_SpiTransfer& request,
                      rpc_pb_SpiTransferResponse& response) {
    const size_t len =
        etl::min(static_cast<size_t>(request.data.size),
                 sizeof(response.data.bytes));
    etl::copy_n(request.data.bytes, len, response.data.bytes);
    const size_t transferred =
        SPIService.transfer(etl::span<uint8_t>(response.data.bytes, len));
    response.data.size = static_cast<pb_size_t>(etl::min(transferred, len));
    return pw::OkStatus();
  }
};
#endif

#if BRIDGE_ENABLE_FILESYSTEM
class FileSystemRpcService final
    : public rpc::pb::pw_rpc::nanopb::FileSystemService::Service<
          FileSystemRpcService> {
 public:
  pw::Status Write(const rpc_pb_FileWrite& request, rpc_pb_Empty&) {
    FileSystem._onWrite(request);
    return pw::OkStatus();
  }

  pw::Status Read(const rpc_pb_FileRead& request,
                  rpc_pb_FileReadResponse& response) {
    return FileSystem._onRead(request, response);
  }

  pw::Status Remove(const rpc_pb_FileRemove& request, rpc_pb_Empty&) {
    FileSystem._onRemove(request);
    return pw::OkStatus();
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
        etl::span<const uint8_t>(request.data.bytes, request.data.size));
    return pw::OkStatus();
  }

  pw::Status Read(const rpc_pb_SubscribeRequest&,
                  rpc_pb_MailboxReadResponse&) {
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

}  // namespace

void BridgeClass::_initObservers() {
  clear_observers();
#if BRIDGE_ENABLE_CONSOLE
  add_observer(Console);
#endif
#if BRIDGE_ENABLE_DATASTORE
  add_observer(DataStore);
#endif
#if BRIDGE_ENABLE_MAILBOX
  add_observer(Mailbox);
#endif
#if BRIDGE_ENABLE_PROCESS
  add_observer(Process);
#endif
#if BRIDGE_ENABLE_FILESYSTEM
  add_observer(FileSystem);
#endif
#if BRIDGE_ENABLE_SPI
  add_observer(SPIService);
#endif
}

BridgeClass::BridgeClass(Stream& stream)
    : _stream(&stream),
      _arduino_stream(stream),
      _rpc_channel_output(*this),
      _rpc_channel(pw::rpc::Channel::Create<1>(&_rpc_channel_output)),
      _rpc_client(pw::span<pw::rpc::Channel>(&_rpc_channel, 1)),
      _rpc_server(pw::span<pw::rpc::Channel>(&_rpc_channel, 1)) {
  _initObservers();
}

BridgeClass::~BridgeClass() {
  if (!_rpc_services_registered) return;
  _rpc_server.UnregisterService(
      g_gpio_service, g_system_service, g_clock_sync_service
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
  );
}

pw::Status BridgeClass::RpcChannelOutput::Send(
    pw::span<const std::byte> packet) {
  const auto data = etl::span<const uint8_t>(
      reinterpret_cast<const uint8_t*>(packet.data()), packet.size());
  return bridge_._writeHdlcFrame(rpc::RPC_HDLC_ADDRESS_RPC, data,
                                 bridge_._rpc_plaintext_response)
             ? pw::OkStatus()
             : pw::Status::Unavailable();
}

void BridgeClass::setStream(Stream& stream) {
  _stream = &stream;
  _arduino_stream.setStream(stream);
#if defined(SERIAL_PORT_LINUXBRIDGE)
  if (_stream == &SERIAL_PORT_LINUXBRIDGE) {
    _hardware_serial = static_cast<HardwareSerial*>(_stream);
    return;
  }
#endif
#if defined(SERIAL_PORT_HARDWARE)
  if (_stream == &SERIAL_PORT_HARDWARE) {
    _hardware_serial = static_cast<HardwareSerial*>(_stream);
    return;
  }
#endif
#if defined(ARDUINO_ARCH_SAMD)
  if (_stream == &Serial) {
    _hardware_serial = nullptr;
    return;
  }
#else
  if (_stream == &Serial) {
    _hardware_serial = static_cast<HardwareSerial*>(_stream);
    return;
  }
#endif
#if defined(Serial1) || defined(HAVE_HWSERIAL1) || \
    (defined(SOC_UART_NUM) && SOC_UART_NUM > 1)
  if (_stream == &Serial1) {
    _hardware_serial = static_cast<HardwareSerial*>(_stream);
    return;
  }
#endif
  _hardware_serial = nullptr;
}

void BridgeClass::_initializeRuntime() {
  _initObservers();
  _timer_last_tick_ms = 0;
  _state_flags.reset();
  _clearLinkSession();

  if (_stream) {
    setStream(*_stream);
  }

  pw_log_avr_SetLogSink(
      [](uint8_t /*level*/, const uint8_t* log_data, size_t size) {
        (void)Bridge.sendLogStream(etl::span<const uint8_t>(log_data, size));
      });

  if (_rpc_services_registered) return;
  _rpc_server.RegisterService(
      g_gpio_service, g_system_service, g_clock_sync_service
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
  );
  _rpc_services_registered = true;
}

void BridgeClass::begin(uint32_t baudrate, const char* secret) {
  _initializeRuntime();

  rpc::security::secure_zero(
      etl::span<uint8_t>(_shared_secret.data(), _shared_secret.size()));
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
  if (!_fsm.is_started()) _fsm.start();
  _fsm.receive(bridge::fsm::EvReset());

  if (secret_too_long) {
    emitStatus(rpc::StatusCode::STATUS_ERROR);
    enterSafeState();
    return;
  }

  _state_flags.set(FLAG_POST_PASSED, bridge::hal::run_power_on_self_tests());
  if (!_state_flags.test(FLAG_POST_PASSED)) {
    enterSafeState();
    return;
  }

  if (baudrate > 0 && _hardware_serial) _hardware_serial->begin(baudrate);
  _state_flags.set(FLAG_TX_ENABLED, true);
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
}

void BridgeClass::_watchdogTask() {
  bridge::hal::watchdog_kick();
  if (_watchdog_timeout_ms > 0 && isSynchronized()) {
    if (NowMillis() - _last_rx_ms > _watchdog_timeout_ms) {
      enterSafeState();
    }
  }
}

void BridgeClass::_serialTask() {
  if (!_stream) return;
  etl::array<pw::byte, bridge::config::RX_BUFFER_SIZE> chunk;
  const auto read_result =
      _arduino_stream.Read(
          pw::ByteSpan(chunk.data(), bridge::config::RX_BUFFER_SIZE));
  if (read_result.ok()) {
    const pw::ConstByteSpan rx_span = read_result.value();
    etl::for_each(rx_span.begin(), rx_span.end(), [this](const pw::byte b) {
      auto result = _hdlc_decoder.Process(b);
      if (result.ok()) {
        _handleReceivedHdlcFrame(result.value());
      }
    });
  }

  const int avail = _stream->available();
  if (!_state_flags.test(FLAG_SERIAL_XOFF) &&
      avail > bridge::config::FLOW_CONTROL_XOFF_THRESHOLD) {
    _state_flags.set(FLAG_SERIAL_XOFF, true);
  } else if (_state_flags.test(FLAG_SERIAL_XOFF) &&
             avail < bridge::config::FLOW_CONTROL_XON_THRESHOLD) {
    _state_flags.set(FLAG_SERIAL_XOFF, false);
  }
}

void BridgeClass::_handleReceivedHdlcFrame(const pw::hdlc::Frame& frame) {
  struct RxPlaintextUseGuard {
    bool& in_use;
    const bool previous;

    explicit RxPlaintextUseGuard(bool& value)
        : in_use(value), previous(value) {}
    ~RxPlaintextUseGuard() { in_use = previous; }
  } rx_plaintext_use_guard(_rx_plaintext_in_use);

  _last_rx_ms = NowMillis();
  const uint64_t addr = frame.address();
  const auto raw_data = etl::span<const uint8_t>(
      reinterpret_cast<const uint8_t*>(frame.data().data()),
      frame.data().size());
  etl::span<const uint8_t> data = raw_data;
  pw::Result<pw::rpc::PacketMeta> meta_result =
      pw::Status::DataLoss();
  bool decrypted = false;

  if (addr == rpc::RPC_HDLC_ADDRESS_RPC) {
    const auto raw_meta = pw::rpc::PacketMeta::FromBuffer(frame.data());
    const bool clear_link_reset =
        raw_meta.ok() && isLinkResetRequest(raw_meta.value());

    if (!_shared_secret.empty() && _session_keys_ready && !clear_link_reset) {
      if (!_decryptHdlcFrame(addr, raw_data, data)) {
        emitStatus(rpc::StatusCode::STATUS_ERROR);
        return;
      }
      decrypted = true;
      _rx_plaintext_in_use = true;
    } else if (!_shared_secret.empty() && !_session_keys_ready) {
      if (!raw_meta.ok() ||
          (!isLinkSyncRequest(raw_meta.value()) && !clear_link_reset)) {
        emitStatus(rpc::StatusCode::STATUS_ERROR);
        enterSafeState();
        return;
      }
    }

    if (!decrypted) {
      meta_result = raw_meta;
    }
  } else if (!_shared_secret.empty()) {
    if (!_session_keys_ready || !_decryptHdlcFrame(addr, raw_data, data)) {
      emitStatus(rpc::StatusCode::STATUS_ERROR);
      return;
    }
    decrypted = true;
    _rx_plaintext_in_use = true;
  }

  if (addr == rpc::RPC_HDLC_ADDRESS_RPC) {
    if (decrypted) {
      meta_result = pw::rpc::PacketMeta::FromBuffer(pw::ConstByteSpan(
          reinterpret_cast<const pw::byte*>(data.data()), data.size()));
    }
    if (!meta_result.ok()) {
      if (decrypted) {
        rpc::security::secure_zero(etl::span<uint8_t>(_rx_plaintext));
      }
      emitStatus(rpc::StatusCode::STATUS_MALFORMED);
      return;
    }
    const auto& meta = meta_result.value();
    if (meta.destination_is_server()) {
      if (!_shared_secret.empty() && _session_keys_ready &&
          isLinkSyncRequest(meta)) {
        emitStatus(rpc::StatusCode::STATUS_ERROR);
        if (decrypted) {
          rpc::security::secure_zero(etl::span<uint8_t>(_rx_plaintext));
        }
        return;
      }
      if (!_shared_secret.empty() && !isSynchronized() &&
          !isLinkSyncRequest(meta) && !isLinkResetRequest(meta)) {
        emitStatus(rpc::StatusCode::STATUS_ERROR);
        enterSafeState();
        if (decrypted) {
          rpc::security::secure_zero(etl::span<uint8_t>(_rx_plaintext));
        }
        return;
      }
      _rpc_plaintext_response = isLinkSyncRequest(meta);
      const auto status = _rpc_server.ProcessPacket(pw::ConstByteSpan(
          reinterpret_cast<const pw::byte*>(data.data()), data.size()));
      _rpc_plaintext_response = false;
      if (!status.ok()) emitStatus(rpc::StatusCode::STATUS_ERROR);
    } else if (meta.destination_is_client()) {
      (void)_rpc_client.ProcessPacket(pw::ConstByteSpan(
          reinterpret_cast<const pw::byte*>(data.data()), data.size()));
    }
  }
#if BRIDGE_ENABLE_CONSOLE
  else if (addr == rpc::RPC_HDLC_ADDRESS_CONSOLE) {
    Console.pushBytes(data.data(), data.size());
  }
#endif
  if (decrypted) {
    rpc::security::secure_zero(etl::span<uint8_t>(_rx_plaintext));
  }
}

bool BridgeClass::_writeHdlcFrame(uint64_t address,
                                  etl::span<const uint8_t> data,
                                  bool plaintext) {
  if (!_stream) return false;
  if (plaintext || _shared_secret.empty()) {
    return pw::hdlc::WriteUIFrame(
               address,
               pw::ConstByteSpan(reinterpret_cast<const pw::byte*>(data.data()),
                                 data.size()),
               _arduino_stream)
        .ok();
  }
  return _writeEncryptedHdlcFrame(address, data);
}

bool BridgeClass::_writeEncryptedHdlcFrame(uint64_t address,
                                           etl::span<const uint8_t> data) {
  if (_rx_plaintext_in_use) {
    return _writeEncryptedHdlcFrameWithStackBuffer(address, data);
  }
  return _writeEncryptedHdlcFrameWithBuffer(address, data,
                                            etl::span<uint8_t>(_rx_plaintext));
}

bool BridgeClass::_writeEncryptedHdlcFrameWithStackBuffer(
    uint64_t address, etl::span<const uint8_t> data) {
  etl::array<uint8_t, bridge::config::RX_BUFFER_SIZE> envelope = {};
  return _writeEncryptedHdlcFrameWithBuffer(address, data,
                                            etl::span<uint8_t>(envelope));
}

bool BridgeClass::_writeEncryptedHdlcFrameWithBuffer(
    uint64_t address, etl::span<const uint8_t> data,
    etl::span<uint8_t> envelope) {
  constexpr size_t kAeadOverhead =
      rpc::RPC_AEAD_COUNTER_SIZE + rpc::RPC_AEAD_TAG_SIZE;
  if (!_session_keys_ready || address > UINT8_MAX ||
      envelope.size() < kAeadOverhead ||
      data.size() > envelope.size() - kAeadOverhead ||
      _tx_aead_counter == UINT64_MAX) {
    return false;
  }

  static_assert(rpc::RPC_AEAD_NONCE_SIZE == 4U + rpc::RPC_AEAD_COUNTER_SIZE);
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> nonce = {};
  etl::array<uint8_t, 1U + rpc::RPC_AEAD_COUNTER_SIZE> aad = {};
  const uint64_t counter = ++_tx_aead_counter;
  aad[0] = static_cast<uint8_t>(address);
  size_t idx = 0;
  etl::for_each(nonce.begin() + 4U, nonce.end(), [&](uint8_t& nonce_byte) {
    const size_t shift = (rpc::RPC_AEAD_COUNTER_SIZE - 1U - idx) * 8U;
    const uint8_t byte = static_cast<uint8_t>(counter >> shift);
    envelope[idx] = byte;
    nonce_byte = byte;
    aad[1U + idx] = byte;
    ++idx;
  });

  const size_t ciphertext_offset = rpc::RPC_AEAD_COUNTER_SIZE;
  auto ciphertext =
      etl::span<uint8_t>(envelope.data() + ciphertext_offset, data.size());
  auto tag =
      etl::span<uint8_t>(envelope.data() + ciphertext_offset + data.size(),
                         rpc::RPC_AEAD_TAG_SIZE);
  (void)rpc::security::aead_encrypt(etl::span<const uint8_t>(_tx_aead_key),
                                    etl::span<const uint8_t>(nonce),
                                    etl::span<const uint8_t>(aad), data,
                                    ciphertext, tag);

  const size_t envelope_size =
      rpc::RPC_AEAD_COUNTER_SIZE + data.size() + rpc::RPC_AEAD_TAG_SIZE;
  const bool written =
      pw::hdlc::WriteUIFrame(
          address,
          pw::ConstByteSpan(reinterpret_cast<const pw::byte*>(envelope.data()),
                            envelope_size),
          _arduino_stream)
          .ok();
  rpc::security::secure_zero(etl::span<uint8_t>(envelope));
  rpc::security::secure_zero(etl::span<uint8_t>(nonce));
  rpc::security::secure_zero(etl::span<uint8_t>(aad));
  return written;
}

bool BridgeClass::_decryptHdlcFrame(uint64_t address,
                                    etl::span<const uint8_t> envelope,
                                    etl::span<const uint8_t>& plaintext) {
  plaintext = {};
  const size_t overhead = rpc::RPC_AEAD_COUNTER_SIZE + rpc::RPC_AEAD_TAG_SIZE;
  if (!_session_keys_ready || address > UINT8_MAX ||
      envelope.size() < overhead ||
      envelope.size() - overhead > _rx_plaintext.size()) {
    rpc::security::secure_zero(etl::span<uint8_t>(_rx_plaintext));
    return false;
  }

  static_assert(rpc::RPC_AEAD_NONCE_SIZE == 4U + rpc::RPC_AEAD_COUNTER_SIZE);
  uint64_t counter = 0;
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> nonce = {};
  etl::array<uint8_t, 1U + rpc::RPC_AEAD_COUNTER_SIZE> aad = {};
  aad[0] = static_cast<uint8_t>(address);
  size_t idx = 0;
  etl::for_each(nonce.begin() + 4U, nonce.end(), [&](uint8_t& nonce_byte) {
    const uint8_t byte = envelope[idx];
    counter = (counter << 8U) | byte;
    nonce_byte = byte;
    aad[1U + idx] = byte;
    ++idx;
  });
  if (counter == 0 || counter <= _rx_aead_counter) {
    rpc::security::secure_zero(etl::span<uint8_t>(nonce));
    rpc::security::secure_zero(etl::span<uint8_t>(aad));
    rpc::security::secure_zero(etl::span<uint8_t>(_rx_plaintext));
    return false;
  }

  const size_t ciphertext_size = envelope.size() - overhead;
  const uint8_t* ciphertext = envelope.data() + rpc::RPC_AEAD_COUNTER_SIZE;
  const uint8_t* tag = ciphertext + ciphertext_size;
  const bool decrypted = rpc::security::aead_decrypt(
      etl::span<const uint8_t>(_rx_aead_key), etl::span<const uint8_t>(nonce),
      etl::span<const uint8_t>(aad),
      etl::span<const uint8_t>(ciphertext, ciphertext_size),
      etl::span<const uint8_t>(tag, rpc::RPC_AEAD_TAG_SIZE),
      etl::span<uint8_t>(_rx_plaintext.data(), ciphertext_size));
  rpc::security::secure_zero(etl::span<uint8_t>(nonce));
  rpc::security::secure_zero(etl::span<uint8_t>(aad));
  if (!decrypted) {
    rpc::security::secure_zero(etl::span<uint8_t>(_rx_plaintext));
    return false;
  }

  _rx_aead_counter = counter;
  plaintext = etl::span<const uint8_t>(_rx_plaintext.data(), ciphertext_size);
  return true;
}

void BridgeClass::_clearLinkSession() {
  _pin_stream_writer = {};
  rpc::security::secure_zero(etl::span<uint8_t>(_tx_aead_key));
  rpc::security::secure_zero(etl::span<uint8_t>(_rx_aead_key));
  _tx_aead_counter = 0;
  _rx_aead_counter = 0;
  _session_keys_ready = false;
}

void BridgeClass::_timerTask() {
  const uint32_t now = NowMillis();
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
  const uint32_t start_ms = NowMillis();
  const uint32_t step_ms = etl::max<uint32_t>(poll_delay_ms, 1U);
  uint8_t cursor = 0U;
  uint8_t sentinel = 0U;
  (void)etl::find_if(etl::fixed_iterator<uint8_t*>(&cursor),
                     etl::fixed_iterator<uint8_t*>(&sentinel),
                     [this, start_ms, timeout_ms, step_ms](uint8_t) {
                       process();
                       if (isSynchronized()) return true;
                       if (NowMillis() - start_ms >= timeout_ms) {
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
  bridge::hal::forceSafeState();
  etl::for_each(_safety_pins.begin(), _safety_pins.end(),
                [](const SafetyPinEntry& entry) {
                  bridge::hal::applySafetyPin(entry.pin, entry.state);
                });
  _pin_stream_writer = {};
  _state_flags.set(FLAG_TX_ENABLED, false);
  _fsm.receive(bridge::fsm::EvReset());
  notify_observers(bridge::SystemEvent::SAFE_STATE_ENTERED);
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

bool BridgeClass::sendConsoleStream(etl::span<const uint8_t> data) {
  if (!_stream || !_state_flags.test(FLAG_TX_ENABLED)) return false;
  return _writeHdlcFrame(rpc::RPC_HDLC_ADDRESS_CONSOLE, data);
}

bool BridgeClass::sendLogStream(etl::span<const uint8_t> data) {
  if (!_stream || !_state_flags.test(FLAG_TX_ENABLED)) return false;
  return _writeHdlcFrame(rpc::RPC_HDLC_ADDRESS_LOG, data);
}

void BridgeClass::_onHandshakeTimeout() {
  if (_fsm.isSynchronized()) return;
  _fsm.receive(bridge::fsm::EvTimeout());
  _state_flags.set(FLAG_TX_ENABLED, false);
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

void BridgeClass::_handleSetPinMode(const rpc_pb_PinMode& m) {
  constexpr etl::array<etl::pair<rpc_pb_PinModeType, uint8_t>, 3> kPinModeMap{{
      {rpc_pb_PinModeType_PIN_INPUT, INPUT},
      {rpc_pb_PinModeType_PIN_OUTPUT, OUTPUT},
      {rpc_pb_PinModeType_PIN_INPUT_PULLUP, INPUT_PULLUP},
  }};
  auto it = etl::find_if(kPinModeMap.begin(), kPinModeMap.end(),
                         [&m](const etl::pair<rpc_pb_PinModeType, uint8_t>& p) {
                           return p.first == m.mode;
                         });
  const uint8_t m_val = (it != kPinModeMap.end()) ? it->second : INPUT;
#if defined(ARDUINO_ARCH_ESP32)
  if (!digitalPinIsValid(m.pin)) return;
#endif
  pinMode(m.pin, m_val);
}

void BridgeClass::_handlePinSubscribe(
    const rpc_pb_PinSubscribeRequest& m,
    pw::rpc::NanopbServerWriter<rpc_pb_PinUpdateEvent>& writer) {
  bool ok = false;
  auto it = etl::find_if(
      _pin_subscriptions.begin(), _pin_subscriptions.end(),
      [pin = m.pin](const PinSubscription& s) { return s.pin == pin; });
  if (!m.enabled) {
    if (it != _pin_subscriptions.end()) {
      it->active = false;
    }
    ok = true;
  } else {
    if (it == _pin_subscriptions.end() && !_pin_subscriptions.full()) {
      _pin_subscriptions.push_back(
          {static_cast<uint8_t>(m.pin), 0, 0, 0, 0, 0, false});
      it = _pin_subscriptions.end() - 1;
    }
    if (it != _pin_subscriptions.end()) {
      it->mode = static_cast<uint8_t>(m.mode);
      it->interval_ms = static_cast<uint16_t>(m.interval_ms);
      it->hysteresis = static_cast<uint16_t>(m.hysteresis);
      it->active = true;
      it->last_report_ms = 0;
      ok = true;
    }
    if (m.mode == rpc_pb_PinModeType_PIN_INPUT_PULLUP) {
      ::pinMode(static_cast<uint8_t>(m.pin), INPUT_PULLUP);
    } else if (m.mode == rpc_pb_PinModeType_PIN_INPUT) {
      ::pinMode(static_cast<uint8_t>(m.pin), INPUT);
    }
  }

  if (!ok) {
    (void)writer.Finish(pw::Status::ResourceExhausted());
    return;
  }

  _pin_stream_writer = etl::move(writer);
  const bool any_active = etl::any_of(
      _pin_subscriptions.begin(), _pin_subscriptions.end(),
      [](const PinSubscription& s) { return s.active; });
  if (!any_active && _pin_stream_writer.active()) {
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
    emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }

  bridge::pb::copy_bytes(response.nonce, request.nonce.bytes,
                         rpc::RPC_HANDSHAKE_NONCE_LENGTH);
  if (!_shared_secret.empty()) {
    if (!rpc::security::handshake_verify_tag(
            etl::span<const uint8_t>(_shared_secret),
            etl::span<const uint8_t>(request.nonce.bytes, request.nonce.size),
            etl::span<const uint8_t>(request.tag.bytes, request.tag.size))) {
      _clearLinkSession();
      _fsm.receive(bridge::fsm::EvHandshakeFailed());
      emitStatus(rpc::StatusCode::STATUS_ERROR);
      return;
    }

    (void)rpc::security::handshake_generate_response_tag(
        etl::span<const uint8_t>(_shared_secret),
        etl::span<const uint8_t>(request.nonce.bytes, request.nonce.size),
        etl::span<uint8_t>(response.tag.bytes, sizeof(response.tag.bytes)));
    (void)rpc::security::derive_session_keys(
        etl::span<const uint8_t>(_shared_secret),
        etl::span<const uint8_t>(request.nonce.bytes, request.nonce.size),
        etl::span<uint8_t>(_rx_aead_key), etl::span<uint8_t>(_tx_aead_key));
    response.tag.size = rpc::RPC_HANDSHAKE_TAG_LENGTH;
    _tx_aead_counter = 0;
    _rx_aead_counter = 0;
    _session_keys_ready = true;
  } else {
    _clearLinkSession();
  }
  _fsm.receive(bridge::fsm::EvHandshakeStart());
  _fsm.receive(bridge::fsm::EvHandshakeComplete());
  _state_flags.set(FLAG_TX_ENABLED, true);
  _timers.stop(bridge::scheduler::TIMER_HANDSHAKE_TIMEOUT);
}

void BridgeClass::_handleLinkReset() {
  _clearLinkSession();
  _state_flags.set(FLAG_TX_ENABLED, true);
  _fsm.receive(bridge::fsm::EvReset());
  _timers.stop(bridge::scheduler::TIMER_HANDSHAKE_TIMEOUT);
  _timers.set_period(bridge::scheduler::TIMER_HANDSHAKE_TIMEOUT,
                     _response_timeout_ms);
  _timers.start(bridge::scheduler::TIMER_HANDSHAKE_TIMEOUT);
}

void BridgeClass::_subscriptionTask() {
  if (!isSynchronized()) return;
  const uint32_t now = NowMillis();
  etl::for_each(_pin_subscriptions.begin(), _pin_subscriptions.end(),
                [this, now](PinSubscription& sub) {
                  if (!sub.active) return;
                  if (now - sub.last_report_ms >= sub.interval_ms) {
                    const uint16_t val =
                        (sub.mode == rpc_pb_PinModeType_PIN_INPUT ||
                         sub.mode == rpc_pb_PinModeType_PIN_INPUT_PULLUP)
                            ? static_cast<uint16_t>(::digitalRead(sub.pin))
                            : static_cast<uint16_t>(::analogRead(sub.pin));
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
  if (!_stream || !_state_flags.test(FLAG_TX_ENABLED) || !isSynchronized() ||
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
