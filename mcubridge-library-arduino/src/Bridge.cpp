#include "Bridge.h"

#include <etl/algorithm.h>
#include <etl/functional.h>
#include <etl/iterator.h>
#include <wolfssl/wolfcrypt/settings.h>
#include <wolfssl/wolfcrypt/types.h>

#include "hal/ArchTraits.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/FileSystem.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "services/SPIService.h"

namespace etl {
void __attribute__((weak)) handle_error(const etl::exception& e) {
  BridgeClass::ErrorPolicy::handle(Bridge, e);
}
}  // namespace etl

BridgeClass::BridgeClass(Stream& stream)
    : _stream(stream),
      _packet_serial(etl::span<uint8_t>(_rx_buffer.data(), _rx_buffer.size()),
                     etl::span<uint8_t>(_rx_buffer.data(), _rx_buffer.size())) {
}

struct CommandVisitor {
  BridgeClass& b;
  const bridge::router::CommandContext& ctx;

  CommandVisitor(BridgeClass& bridge,
                 const bridge::router::CommandContext& context)
      : b(bridge), ctx(context) {}

  void operator()(const etl::monostate&) const {
    switch (ctx.raw_command) {
      case rpc::to_underlying(rpc::StatusCode::STATUS_OK):
        if (ctx.is_duplicate) {
          b._processAck(ctx.raw_command, ctx.sequence_id);
          return;
        }
        b._processAck(ctx.raw_command, ctx.sequence_id);
        b._handleStatusOk(ctx);
        break;

      case rpc::to_underlying(rpc::StatusCode::STATUS_MALFORMED):
        b._handleStatusMalformed(ctx);
        break;

      case rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION):
        if (ctx.is_duplicate) {
          b._retransmitLastFrame();
          return;
        }
        b._handleGetVersion(ctx);
        break;

      case rpc::to_underlying(rpc::CommandId::CMD_GET_FREE_MEMORY):
        if (ctx.is_duplicate) {
          b._retransmitLastFrame();
          return;
        }
        b._handleGetFreeMemory(ctx);
        break;

      case rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET):
        if (ctx.is_duplicate) {
          b._processAck(ctx.raw_command, ctx.sequence_id);
          return;
        }
        b._processAck(ctx.raw_command, ctx.sequence_id);
        b._handleLinkReset(ctx);
        break;

      case rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES):
        if (ctx.is_duplicate) {
          b._retransmitLastFrame();
          return;
        }
        b._handleGetCapabilities(ctx);
        break;

      case rpc::to_underlying(rpc::CommandId::CMD_XOFF):
        if (ctx.is_duplicate) {
          b._processAck(ctx.raw_command, ctx.sequence_id);
          return;
        }
        b._processAck(ctx.raw_command, ctx.sequence_id);
        b._handleXoff(ctx);
        break;

      case rpc::to_underlying(rpc::CommandId::CMD_XON):
        if (ctx.is_duplicate) {
          b._processAck(ctx.raw_command, ctx.sequence_id);
          return;
        }
        b._processAck(ctx.raw_command, ctx.sequence_id);
        b._handleXon(ctx);
        break;

#if BRIDGE_ENABLE_SPI
      case rpc::to_underlying(rpc::CommandId::CMD_SPI_BEGIN):
        if (ctx.is_duplicate) {
          b._processAck(ctx.raw_command, ctx.sequence_id);
          return;
        }
        b._processAck(ctx.raw_command, ctx.sequence_id);
        b._handleSpiBegin(ctx);
        break;

      case rpc::to_underlying(rpc::CommandId::CMD_SPI_END):
        if (ctx.is_duplicate) {
          b._processAck(ctx.raw_command, ctx.sequence_id);
          return;
        }
        b._processAck(ctx.raw_command, ctx.sequence_id);
        b._handleSpiEnd(ctx);
        break;
#endif

      default:
        b.onUnknownCommand(ctx);
        break;
    }
  }

  void operator()(const rpc_pb_AckPacket& m) const {
    if (ctx.is_duplicate) {
      b._processAck(ctx.raw_command, ctx.sequence_id);
      return;
    }
    b._processAck(ctx.raw_command, ctx.sequence_id);
    b._handleStatusAck(ctx, m);
  }

  void operator()(const rpc_pb_LinkSync& m) const {
    if (ctx.is_duplicate) {
      b._processAck(ctx.raw_command, ctx.sequence_id);
      return;
    }
    b._processAck(ctx.raw_command, ctx.sequence_id);
    b._handleLinkSync(ctx, m);
  }

  void operator()(const rpc_pb_SetBaudratePacket& m) const {
    if (ctx.is_duplicate) {
      b._processAck(ctx.raw_command, ctx.sequence_id);
      return;
    }
    b._processAck(ctx.raw_command, ctx.sequence_id);
    b._handleSetBaudrate(m);
  }

  void operator()(const rpc_pb_EnterBootloader& m) const {
    if (ctx.is_duplicate) {
      b._processAck(ctx.raw_command, ctx.sequence_id);
      return;
    }
    b._processAck(ctx.raw_command, ctx.sequence_id);
    b._handleEnterBootloader(m);
  }

  void operator()(const rpc_pb_PinMode& m) const {
    if (ctx.is_duplicate) {
      b._processAck(ctx.raw_command, ctx.sequence_id);
      return;
    }
    b._processAck(ctx.raw_command, ctx.sequence_id);
    b._handleSetPinMode(m);
  }

  void operator()(const rpc_pb_DigitalWrite& m) const {
    if (ctx.is_duplicate) {
      b._processAck(ctx.raw_command, ctx.sequence_id);
      return;
    }
    b._processAck(ctx.raw_command, ctx.sequence_id);
    b._handleDigitalWrite(m);
  }

  void operator()(const rpc_pb_AnalogWrite& m) const {
    if (ctx.is_duplicate) {
      b._processAck(ctx.raw_command, ctx.sequence_id);
      return;
    }
    b._processAck(ctx.raw_command, ctx.sequence_id);
    b._handleAnalogWrite(m);
  }

  void operator()(const rpc_pb_PinRead& m) const {
    if (ctx.is_duplicate) {
      b._retransmitLastFrame();
      return;
    }
    if (ctx.raw_command ==
        rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ)) {
      b._handleDigitalRead(ctx, m);
    } else {
      b._handleAnalogRead(ctx, m);
    }
  }

  void operator()(const rpc_pb_ConsoleWrite& m) const {
    if (ctx.is_duplicate) {
      b._processAck(ctx.raw_command, ctx.sequence_id);
      return;
    }
    b._processAck(ctx.raw_command, ctx.sequence_id);
    b._handleConsoleWrite(m);
  }

#if BRIDGE_ENABLE_DATASTORE
  void operator()(const rpc_pb_DatastoreGetResponse& m) const {
    if (ctx.is_duplicate) {
      b._processAck(ctx.raw_command, ctx.sequence_id);
      return;
    }
    b._processAck(ctx.raw_command, ctx.sequence_id);
    b._handleDataStoreGetResponse(ctx, m);
  }
#endif

#if BRIDGE_ENABLE_MAILBOX
  void operator()(const rpc_pb_MailboxPush& m) const {
    if (ctx.is_duplicate) {
      b._processAck(ctx.raw_command, ctx.sequence_id);
      return;
    }
    b._processAck(ctx.raw_command, ctx.sequence_id);
    b._handleMailboxPush(ctx, m);
  }

  void operator()(const rpc_pb_MailboxReadResponse& m) const {
    b._handleMailboxReadResponse(m);
  }

  void operator()(const rpc_pb_MailboxAvailableResponse& m) const {
    b._handleMailboxAvailableResponse(m);
  }
#endif

#if BRIDGE_ENABLE_FILESYSTEM
  void operator()(const rpc_pb_FileWrite& m) const {
    if (ctx.is_duplicate) {
      b._processAck(ctx.raw_command, ctx.sequence_id);
      return;
    }
    b._processAck(ctx.raw_command, ctx.sequence_id);
    b._handleFileWrite(ctx, m);
  }

  void operator()(const rpc_pb_FileRead& m) const {
    if (ctx.is_duplicate) {
      b._processAck(ctx.raw_command, ctx.sequence_id);
      return;
    }
    b._processAck(ctx.raw_command, ctx.sequence_id);
    b._handleFileRead(ctx, m);
  }

  void operator()(const rpc_pb_FileRemove& m) const {
    if (ctx.is_duplicate) {
      b._processAck(ctx.raw_command, ctx.sequence_id);
      return;
    }
    b._processAck(ctx.raw_command, ctx.sequence_id);
    b._handleFileRemove(ctx, m);
  }

  void operator()(const rpc_pb_FileReadResponse& m) const {
    if (ctx.is_duplicate) {
      b._processAck(ctx.raw_command, ctx.sequence_id);
      return;
    }
    b._processAck(ctx.raw_command, ctx.sequence_id);
    b._handleFileReadResponse(ctx, m);
  }
#endif

#if BRIDGE_ENABLE_PROCESS
  void operator()(const rpc_pb_ProcessKill& m) const {
    if (ctx.is_duplicate) {
      b._processAck(ctx.raw_command, ctx.sequence_id);
      return;
    }
    b._processAck(ctx.raw_command, ctx.sequence_id);
    b._handleProcessKill(ctx, m);
  }

  void operator()(const rpc_pb_ProcessRunAsyncResponse& m) const {
    if (ctx.is_duplicate) {
      b._processAck(ctx.raw_command, ctx.sequence_id);
      return;
    }
    b._processAck(ctx.raw_command, ctx.sequence_id);
    b._handleProcessRunAsyncResponse(ctx, m);
  }

  void operator()(const rpc_pb_ProcessPollResponse& m) const {
    if (ctx.is_duplicate) {
      b._processAck(ctx.raw_command, ctx.sequence_id);
      return;
    }
    b._processAck(ctx.raw_command, ctx.sequence_id);
    b._handleProcessPollResponse(ctx, m);
  }
#endif

#if BRIDGE_ENABLE_SPI
  void operator()(const rpc_pb_SpiTransfer& m) const {
    if (ctx.is_duplicate) {
      b._retransmitLastFrame();
      return;
    }
    b._handleSpiTransfer(ctx, m);
  }

  void operator()(const rpc_pb_SpiConfig& m) const {
    if (ctx.is_duplicate) {
      b._processAck(ctx.raw_command, ctx.sequence_id);
      return;
    }
    b._processAck(ctx.raw_command, ctx.sequence_id);
    b._handleSpiSetConfig(m);
  }
#endif
};

void BridgeClass::_dispatchCommand(const rpc_pb_RpcEnvelope& envelope) {
  const uint16_t cmd_id = envelope.command_id;
  auto it =
      etl::find(_rx_history.begin(), _rx_history.end(), envelope.sequence_id);
  const bool is_duplicate = (it != _rx_history.end());
  const bridge::router::CommandContext ctx(&envelope, cmd_id,
                                           envelope.sequence_id, is_duplicate,
                                           rpc::requires_ack(cmd_id));

  if (!is_duplicate) {
    if (_rx_history.full()) _rx_history.pop();
    _rx_history.push(envelope.sequence_id);
  }

  if (!_isSecurityCheckPassed(ctx.raw_command)) {
    if (!sendFrame(rpc::StatusCode::STATUS_ERROR, ctx.sequence_id)) {
    }
    return;
  }

  bridge::router::DecodedResult result = _decodePayloadToVariant(ctx);
  if (!result.success) {
    emitStatus(rpc::StatusCode::STATUS_MALFORMED);
    return;
  }

  CommandVisitor visitor(*this, ctx);
  etl::visit(visitor, result.command);
}

bridge::router::DecodedResult BridgeClass::_decodePayloadToVariant(
    const bridge::router::CommandContext& ctx) {
  bridge::router::DecodedResult res;
  res.success = false;
  res.command = etl::monostate{};

  switch (ctx.raw_command) {
    // Commands with NO payload:
    case rpc::to_underlying(rpc::StatusCode::STATUS_OK):
    case rpc::to_underlying(rpc::StatusCode::STATUS_MALFORMED):
    case rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION):
    case rpc::to_underlying(rpc::CommandId::CMD_GET_FREE_MEMORY):
    case rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET):
    case rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES):
    case rpc::to_underlying(rpc::CommandId::CMD_XOFF):
    case rpc::to_underlying(rpc::CommandId::CMD_XON):
#if BRIDGE_ENABLE_SPI
    case rpc::to_underlying(rpc::CommandId::CMD_SPI_BEGIN):
    case rpc::to_underlying(rpc::CommandId::CMD_SPI_END):
#endif
      res.success = true;
      res.command = etl::monostate{};
      break;

    // Commands with payload:
    case rpc::to_underlying(rpc::StatusCode::STATUS_ACK): {
      rpc_pb_AckPacket m = {};
      if (_decodePayload(ctx, rpc::Payload::get_fields<rpc_pb_AckPacket>(), &m,
                         rpc::Payload::get_tag<rpc_pb_AckPacket>(),
                         sizeof(rpc_pb_AckPacket))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
    case rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC): {
      rpc_pb_LinkSync m = {};
      if (_decodePayload(ctx, rpc::Payload::get_fields<rpc_pb_LinkSync>(), &m,
                         rpc::Payload::get_tag<rpc_pb_LinkSync>(),
                         sizeof(rpc_pb_LinkSync))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
    case rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE): {
      rpc_pb_SetBaudratePacket m = {};
      if (_decodePayload(ctx,
                         rpc::Payload::get_fields<rpc_pb_SetBaudratePacket>(),
                         &m, rpc::Payload::get_tag<rpc_pb_SetBaudratePacket>(),
                         sizeof(rpc_pb_SetBaudratePacket))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
    case rpc::to_underlying(rpc::CommandId::CMD_ENTER_BOOTLOADER): {
      rpc_pb_EnterBootloader m = {};
      if (_decodePayload(ctx,
                         rpc::Payload::get_fields<rpc_pb_EnterBootloader>(), &m,
                         rpc::Payload::get_tag<rpc_pb_EnterBootloader>(),
                         sizeof(rpc_pb_EnterBootloader))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
    case rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE): {
      rpc_pb_PinMode m = {};
      if (_decodePayload(ctx, rpc::Payload::get_fields<rpc_pb_PinMode>(), &m,
                         rpc::Payload::get_tag<rpc_pb_PinMode>(),
                         sizeof(rpc_pb_PinMode))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
    case rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE): {
      rpc_pb_DigitalWrite m = {};
      if (_decodePayload(ctx, rpc::Payload::get_fields<rpc_pb_DigitalWrite>(),
                         &m, rpc::Payload::get_tag<rpc_pb_DigitalWrite>(),
                         sizeof(rpc_pb_DigitalWrite))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
    case rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE): {
      rpc_pb_AnalogWrite m = {};
      if (_decodePayload(ctx, rpc::Payload::get_fields<rpc_pb_AnalogWrite>(),
                         &m, rpc::Payload::get_tag<rpc_pb_AnalogWrite>(),
                         sizeof(rpc_pb_AnalogWrite))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
    case rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ):
    case rpc::to_underlying(rpc::CommandId::CMD_ANALOG_READ): {
      rpc_pb_PinRead m = {};
      if (_decodePayload(ctx, rpc::Payload::get_fields<rpc_pb_PinRead>(), &m,
                         rpc::Payload::get_tag<rpc_pb_PinRead>(),
                         sizeof(rpc_pb_PinRead))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
    case rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE): {
      rpc_pb_ConsoleWrite m = {};
      if (_decodePayload(ctx, rpc::Payload::get_fields<rpc_pb_ConsoleWrite>(),
                         &m, rpc::Payload::get_tag<rpc_pb_ConsoleWrite>(),
                         sizeof(rpc_pb_ConsoleWrite))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
#if BRIDGE_ENABLE_DATASTORE
    case rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP): {
      rpc_pb_DatastoreGetResponse m = {};
      if (_decodePayload(
              ctx, rpc::Payload::get_fields<rpc_pb_DatastoreGetResponse>(), &m,
              rpc::Payload::get_tag<rpc_pb_DatastoreGetResponse>(),
              sizeof(rpc_pb_DatastoreGetResponse))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
#endif
#if BRIDGE_ENABLE_MAILBOX
    case rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH): {
      rpc_pb_MailboxPush m = {};
      if (_decodePayload(ctx, rpc::Payload::get_fields<rpc_pb_MailboxPush>(),
                         &m, rpc::Payload::get_tag<rpc_pb_MailboxPush>(),
                         sizeof(rpc_pb_MailboxPush))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
    case rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP): {
      rpc_pb_MailboxReadResponse m = {};
      if (_decodePayload(
              ctx, rpc::Payload::get_fields<rpc_pb_MailboxReadResponse>(), &m,
              rpc::Payload::get_tag<rpc_pb_MailboxReadResponse>(),
              sizeof(rpc_pb_MailboxReadResponse))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
    case rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP): {
      rpc_pb_MailboxAvailableResponse m = {};
      if (_decodePayload(
              ctx, rpc::Payload::get_fields<rpc_pb_MailboxAvailableResponse>(),
              &m, rpc::Payload::get_tag<rpc_pb_MailboxAvailableResponse>(),
              sizeof(rpc_pb_MailboxAvailableResponse))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
#endif
#if BRIDGE_ENABLE_FILESYSTEM
    case rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE): {
      rpc_pb_FileWrite m = {};
      if (_decodePayload(ctx, rpc::Payload::get_fields<rpc_pb_FileWrite>(), &m,
                         rpc::Payload::get_tag<rpc_pb_FileWrite>(),
                         sizeof(rpc_pb_FileWrite))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
    case rpc::to_underlying(rpc::CommandId::CMD_FILE_READ): {
      rpc_pb_FileRead m = {};
      if (_decodePayload(ctx, rpc::Payload::get_fields<rpc_pb_FileRead>(), &m,
                         rpc::Payload::get_tag<rpc_pb_FileRead>(),
                         sizeof(rpc_pb_FileRead))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
    case rpc::to_underlying(rpc::CommandId::CMD_FILE_REMOVE): {
      rpc_pb_FileRemove m = {};
      if (_decodePayload(ctx, rpc::Payload::get_fields<rpc_pb_FileRemove>(), &m,
                         rpc::Payload::get_tag<rpc_pb_FileRemove>(),
                         sizeof(rpc_pb_FileRemove))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
    case rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP): {
      rpc_pb_FileReadResponse m = {};
      if (_decodePayload(ctx,
                         rpc::Payload::get_fields<rpc_pb_FileReadResponse>(),
                         &m, rpc::Payload::get_tag<rpc_pb_FileReadResponse>(),
                         sizeof(rpc_pb_FileReadResponse))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
#endif
#if BRIDGE_ENABLE_PROCESS
    case rpc::to_underlying(rpc::CommandId::CMD_PROCESS_KILL): {
      rpc_pb_ProcessKill m = {};
      if (_decodePayload(ctx, rpc::Payload::get_fields<rpc_pb_ProcessKill>(),
                         &m, rpc::Payload::get_tag<rpc_pb_ProcessKill>(),
                         sizeof(rpc_pb_ProcessKill))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
    case rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP): {
      rpc_pb_ProcessRunAsyncResponse m = {};
      if (_decodePayload(
              ctx, rpc::Payload::get_fields<rpc_pb_ProcessRunAsyncResponse>(),
              &m, rpc::Payload::get_tag<rpc_pb_ProcessRunAsyncResponse>(),
              sizeof(rpc_pb_ProcessRunAsyncResponse))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
    case rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP): {
      rpc_pb_ProcessPollResponse m = {};
      if (_decodePayload(
              ctx, rpc::Payload::get_fields<rpc_pb_ProcessPollResponse>(), &m,
              rpc::Payload::get_tag<rpc_pb_ProcessPollResponse>(),
              sizeof(rpc_pb_ProcessPollResponse))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
#endif
#if BRIDGE_ENABLE_SPI
    case rpc::to_underlying(rpc::CommandId::CMD_SPI_TRANSFER): {
      rpc_pb_SpiTransfer m = {};
      if (_decodePayload(ctx, rpc::Payload::get_fields<rpc_pb_SpiTransfer>(),
                         &m, rpc::Payload::get_tag<rpc_pb_SpiTransfer>(),
                         sizeof(rpc_pb_SpiTransfer))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
    case rpc::to_underlying(rpc::CommandId::CMD_SPI_SET_CONFIG): {
      rpc_pb_SpiConfig m = {};
      if (_decodePayload(ctx, rpc::Payload::get_fields<rpc_pb_SpiConfig>(), &m,
                         rpc::Payload::get_tag<rpc_pb_SpiConfig>(),
                         sizeof(rpc_pb_SpiConfig))) {
        res.success = true;
        res.command = m;
      }
      break;
    }
#endif

    default:
      res.success = true;
      res.command = etl::monostate{};
      break;
  }

  return res;
}

void BridgeClass::_initializeRuntime() {
  _timer_last_tick_ms = 0;
  _serial_xoff_sent = false;

  // Shared buffer initialized by PacketSerial

  _rx_buffer.fill(0);
  if constexpr (bridge::hal::CurrentArchTraits::id ==
                bridge::hal::ArchId::ARCH_AVR)
    _hardware_serial = static_cast<HardwareSerial*>(&_stream);
  else
    _hardware_serial = nullptr;
}

void BridgeClass::begin(uint32_t baudrate, const char* secret) {
  _initializeRuntime();

  wolfCrypt_Init();
  _shared_secret.clear();
  if (secret != nullptr) {
    const etl::string_view s(secret);
    const size_t len = etl::min(s.size(), _shared_secret.capacity());
    const auto data_ptr =
        static_cast<const uint8_t*>(static_cast<const void*>(s.data()));
    _shared_secret.assign(data_ptr, data_ptr + len);
  }
  bridge::hal::init();
  if (!_fsm.is_started()) _fsm.start();
  _fsm.receive(bridge::fsm::EvReset());
#if BRIDGE_ENABLE_POST_TESTS
  _is_post_passed = rpc::security::run_cryptographic_self_tests();
  if (!_is_post_passed) enterSafeState();
#else
  _is_post_passed = true;
#endif
  if constexpr (bridge::hal::CurrentArchTraits::id ==
                bridge::hal::ArchId::ARCH_AVR)
    if (baudrate > 0 && _hardware_serial) _hardware_serial->begin(baudrate);
  _tx_enabled = true;
  _timers.clear();
  _timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT] =
      _timers.register_timer([]() { Bridge._onAckTimeout(); }, _ack_timeout_ms,
                             etl::timer::mode::REPEATING);
  _timer_ids[bridge::scheduler::TIMER_RX_DEDUPE] = _timers.register_timer(
      []() { Bridge._onRxDedupe(); }, bridge::config::RX_DEDUPE_INTERVAL_MS,
      etl::timer::mode::REPEATING);
  _timer_ids[bridge::scheduler::TIMER_BAUDRATE_CHANGE] = _timers.register_timer(
      []() { Bridge._onBaudrateChange(); },
      bridge::config::BAUDRATE_CHANGE_DELAY_MS, etl::timer::mode::SINGLE_SHOT);
  _timer_ids[bridge::scheduler::TIMER_BOOTLOADER_DELAY] =
      _timers.register_timer([]() { Bridge._onBootloaderDelay(); },
                             bridge::config::BOOTLOADER_DELAY_MS,
                             etl::timer::mode::SINGLE_SHOT);
  _packet_serial.setPacketHandler(
      etl::delegate<void(etl::span<const uint8_t>)>::create<
          BridgeClass, &BridgeClass::_handleReceivedFrame>(*this));
}

void BridgeClass::process() {
  _watchdogTask();
  _serialTask();
  _timerTask();
  if constexpr (bridge::config::ENABLE_MAILBOX) Mailbox.process();
}
void BridgeClass::_watchdogTask() { bridge::hal::watchdog_kick(); }

void BridgeClass::_serialTask() {
  _packet_serial.update(_stream);
  const int avail = _stream.available();
  if (!_serial_xoff_sent &&
      avail > bridge::config::FLOW_CONTROL_XOFF_THRESHOLD) {
    signalXoff();
    _serial_xoff_sent = true;
  } else if (_serial_xoff_sent &&
             avail < bridge::config::FLOW_CONTROL_XON_THRESHOLD) {
    signalXon();
    _serial_xoff_sent = false;
  }
}

void BridgeClass::_timerTask() {
  const uint32_t now = ::millis();
  if (_timer_last_tick_ms == 0) _timer_last_tick_ms = now;
  const uint32_t elapsed = now - _timer_last_tick_ms;
  if (elapsed > 0) {
    _timers.tick(elapsed);
    _timer_last_tick_ms = now;
  }
}
bool BridgeClass::isSynchronized() const { return _fsm.isSynchronized(); }
void BridgeClass::_handleStatusOk(const bridge::router::CommandContext&) {}
void BridgeClass::onUnknownCommand(const bridge::router::CommandContext& ctx) {
  if (_command_handler.is_valid())
    _command_handler(*ctx.envelope);
  else
    emitStatus(rpc::StatusCode::STATUS_ERROR);
}

void BridgeClass::enterSafeState() {
  bridge::hal::forceSafeState();
  _tx_enabled = false;
  _clearPendingTxQueue();
  _fsm.receive(bridge::fsm::EvReset());
  Console.onLost();
  DataStore.onLost();
  Mailbox.onLost();
  Process.onLost();
  FileSystem.onLost();
  SPIService.onLost();
}

void BridgeClass::_serialize_and_send(const rpc_pb_RpcEnvelope& env) {
  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> buffer;
  const size_t len = rpc::serialize_frame(env, buffer);
  if (len > 0)
    _packet_serial.send(_stream, etl::span<const uint8_t>(buffer.data(), len));
}

bool BridgeClass::_sendFrameRaw(const rpc_pb_RpcEnvelope& env,
                                uint16_t command_id) {
  if (!_tx_enabled && !rpc::is_system_command(command_id)) return false;
  _serialize_and_send(env);
  return true;
}

void BridgeClass::_transmit(uint16_t command_id, uint16_t sequence_id,
                            etl::span<const uint8_t> payload) {
  const uint16_t raw_cmd = command_id;
  const bool is_excluded = rpc::is_system_command(raw_cmd);
  const bool do_encrypt =
      isSynchronized() && !_shared_secret.empty() && !is_excluded;
  etl::array<uint8_t, rpc::AEAD_NONCE_SIZE> nonce = {};
  etl::array<uint8_t, rpc::AEAD_TAG_SIZE> tag = {};
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> enc_pl;
  etl::span<const uint8_t> final_payload = payload;
  if (do_encrypt) {
    if (!rpc::security::aead_encrypt_frame(raw_cmd, sequence_id, payload,
                                           _session_key, &_tx_nonce_counter,
                                           enc_pl, nonce, tag))
      return;
    final_payload = etl::span<const uint8_t>(enc_pl.data(), payload.size());
  }
  rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
  env.version = rpc::PROTOCOL_VERSION;
  env.command_id = command_id;
  env.sequence_id = sequence_id;
  etl::copy_n(nonce.begin(), rpc::AEAD_NONCE_SIZE, env.nonce.bytes);
  env.nonce.size = static_cast<pb_size_t>(rpc::AEAD_NONCE_SIZE);
  const size_t pl_size = etl::min(final_payload.size(),
                                  static_cast<size_t>(rpc::MAX_PAYLOAD_SIZE));
  env.which_payload_type = rpc_pb_RpcEnvelope_encrypted_payload_with_tag_tag;
  if (do_encrypt) {
    etl::copy_n(final_payload.begin(), pl_size,
                env.payload_type.encrypted_payload_with_tag.bytes);
    etl::copy_n(tag.begin(), rpc::AEAD_TAG_SIZE,
                env.payload_type.encrypted_payload_with_tag.bytes + pl_size);
    env.payload_type.encrypted_payload_with_tag.size =
        static_cast<pb_size_t>(pl_size + rpc::AEAD_TAG_SIZE);
  } else {
    etl::copy_n(final_payload.begin(), pl_size,
                env.payload_type.encrypted_payload_with_tag.bytes);
    env.payload_type.encrypted_payload_with_tag.size =
        static_cast<pb_size_t>(pl_size);
  }
  _serialize_and_send(env);
}

void BridgeClass::_flushPendingTxQueue() {
  BRIDGE_ATOMIC_BLOCK {
    if (_pending_tx_queue.empty() || !_tx_enabled) return;
    const auto& f = _pending_tx_queue.front();
    _last_command_id = f.command_id;
    _retry_count = 0;
    _fsm.receive(bridge::fsm::EvSendCritical());
    _transmit(f.command_id, f.sequence_id,
              etl::span<const uint8_t>(f.buffer->data.data(), f.length));
    _timers.start(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]);
  }
}

void BridgeClass::_retransmitLastFrame() {
  BRIDGE_ATOMIC_BLOCK {
    if (_pending_tx_queue.empty()) return;
    const auto& f = _pending_tx_queue.front();
    _transmit(f.command_id, f.sequence_id,
              etl::span<const uint8_t>(f.buffer->data.data(), f.length));
    _timers.start(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]);
  }
}

void BridgeClass::_onAckTimeout() {
  if (!_fsm.isAwaitingAck()) return;
  if (++_retry_count >= _retry_limit) {
    _timers.stop(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]);
    _fsm.receive(bridge::fsm::EvTimeout());
    _tx_enabled = false;
    _clearPendingTxQueue();
    return;
  }
  _retransmitLastFrame();
}

void BridgeClass::_processAck(uint16_t command_id, uint16_t sequence_id) {
  rpc_pb_AckPacket p = {};
  p.command_id = command_id;
  if (!send(rpc::StatusCode::STATUS_ACK, sequence_id, p)) {
  }
}

void BridgeClass::_handleAck(uint16_t cmd) {
  if (!_fsm.isAwaitingAck() || cmd != _last_command_id) return;
  _timers.stop(_timer_ids[bridge::scheduler::TIMER_ACK_TIMEOUT]);
  _clearPendingTxQueue();
  _fsm.receive(bridge::fsm::EvAckReceived());
  _flushPendingTxQueue();
}

void BridgeClass::_clearPendingTxQueue() {
  BRIDGE_ATOMIC_BLOCK {
    etl::for_each(_pending_tx_queue.begin(), _pending_tx_queue.end(),
                  [this](PendingTxFrame& f) {
                    if (f.buffer) _tx_payload_pool.release(f.buffer);
                  });
    _pending_tx_queue.clear();
  }
}

void BridgeClass::_onRxDedupe() { _rx_history.clear(); }
void BridgeClass::_onBaudrateChange() {
  if (_pending_baudrate > 0) {
    if (_hardware_serial) _hardware_serial->begin(_pending_baudrate);
    _pending_baudrate = 0;
  }
}
void BridgeClass::_onBootloaderDelay() { bridge::hal::enterBootloader(); }

void BridgeClass::_handleSetBaudrate(const rpc_pb_SetBaudratePacket& msg) {
  if (msg.baudrate == 0 || msg.baudrate == _pending_baudrate) return;
  _pending_baudrate = msg.baudrate;
  _timers.start(_timer_ids[bridge::scheduler::TIMER_BAUDRATE_CHANGE]);
}

void BridgeClass::_handleEnterBootloader(const rpc_pb_EnterBootloader& msg) {
  if (msg.magic == rpc::RPC_BOOTLOADER_MAGIC)
    _timers.start(_timer_ids[bridge::scheduler::TIMER_BOOTLOADER_DELAY]);
}

void BridgeClass::_handleSetPinMode(const rpc_pb_PinMode& m) {
  uint8_t m_val = INPUT;
  if (m.mode == rpc_pb_PinModeType_PIN_OUTPUT)
    m_val = OUTPUT;
  else if (m.mode == rpc_pb_PinModeType_PIN_INPUT_PULLUP)
    m_val = INPUT_PULLUP;
  pinMode(m.pin, m_val);
}

void BridgeClass::_handleDigitalWrite(const rpc_pb_DigitalWrite& m) {
  digitalWrite(m.pin, (m.value == 0) ? LOW : HIGH);
}
void BridgeClass::_handleAnalogWrite(const rpc_pb_AnalogWrite& m) {
  analogWrite(m.pin, (int)m.value);
}

void BridgeClass::_handleDigitalRead(const bridge::router::CommandContext& ctx,
                                     const rpc_pb_PinRead& m) {
  if (m.pin < bridge::config::DIGITAL_PINS) {
    rpc_pb_DigitalReadResponse resp = rpc_pb_DigitalReadResponse_init_default;
    resp.value = static_cast<uint32_t>(::digitalRead(m.pin));
    if (!send(rpc::CommandId::CMD_DIGITAL_READ_RESP, ctx.sequence_id, resp)) {
    }
  } else
    emitStatus(rpc::StatusCode::STATUS_ERROR);
}

void BridgeClass::_handleAnalogRead(const bridge::router::CommandContext& ctx,
                                    const rpc_pb_PinRead& m) {
  if (m.pin < bridge::config::DIGITAL_PINS) {
    rpc_pb_AnalogReadResponse resp = rpc_pb_AnalogReadResponse_init_default;
    resp.value = static_cast<uint32_t>(::analogRead(m.pin));
    if (!send(rpc::CommandId::CMD_ANALOG_READ_RESP, ctx.sequence_id, resp)) {
    }
  } else
    emitStatus(rpc::StatusCode::STATUS_ERROR);
}

void BridgeClass::_handleConsoleWrite(const rpc_pb_ConsoleWrite& m) {
  Console._push(m);
}

#if BRIDGE_ENABLE_DATASTORE
void BridgeClass::_handleDataStoreGetResponse(
    const bridge::router::CommandContext&,
    const rpc_pb_DatastoreGetResponse& m) {
  DataStore._onResponse(m);
}
#endif

#if BRIDGE_ENABLE_MAILBOX
void BridgeClass::_handleMailboxPush(const bridge::router::CommandContext&,
                                     const rpc_pb_MailboxPush& m) {
  MailboxClass<>::_onPush(m);
}
void BridgeClass::_handleMailboxReadResponse(
    const rpc_pb_MailboxReadResponse& m) {
  MailboxClass<>::_onReadResponse(m);
}
void BridgeClass::_handleMailboxAvailableResponse(
    const rpc_pb_MailboxAvailableResponse& m) {
  MailboxClass<>::_onAvailableResponse(m);
}
#endif

#if BRIDGE_ENABLE_FILESYSTEM
void BridgeClass::_handleFileWrite(const bridge::router::CommandContext&,
                                   const rpc_pb_FileWrite& m) {
  FileSystem._onWrite(m);
}
void BridgeClass::_handleFileRead(const bridge::router::CommandContext&,
                                  const rpc_pb_FileRead& m) {
  FileSystem._onRead(m);
}
void BridgeClass::_handleFileRemove(const bridge::router::CommandContext&,
                                    const rpc_pb_FileRemove& m) {
  FileSystem._onRemove(m);
}
void BridgeClass::_handleFileReadResponse(const bridge::router::CommandContext&,
                                          const rpc_pb_FileReadResponse& m) {
  FileSystem._onResponse(m);
}
#endif
#if BRIDGE_ENABLE_PROCESS
void BridgeClass::_handleProcessKill(const bridge::router::CommandContext&,
                                     const rpc_pb_ProcessKill& m) {
  Process._onKillNotification(m);
}
void BridgeClass::_handleProcessRunAsyncResponse(
    const bridge::router::CommandContext&,
    const rpc_pb_ProcessRunAsyncResponse& m) {
  Process._onRunAsyncResponse(m);
}
void BridgeClass::_handleProcessPollResponse(
    const bridge::router::CommandContext&,
    const rpc_pb_ProcessPollResponse& m) {
  Process._onPollResponse(m);
}
#endif
#if BRIDGE_ENABLE_SPI
void BridgeClass::_handleSpiSetConfig(const rpc_pb_SpiConfig& m) {
  SPIService.setConfig(m);
}
void BridgeClass::_handleSpiBegin(const bridge::router::CommandContext& ctx) {
  SPIService.begin();
  _processAck(ctx.raw_command, ctx.sequence_id);
}
void BridgeClass::_handleSpiEnd(const bridge::router::CommandContext& ctx) {
  SPIService.end();
  _processAck(ctx.raw_command, ctx.sequence_id);
}
void BridgeClass::_handleSpiTransfer(const bridge::router::CommandContext& ctx,
                                     const rpc_pb_SpiTransfer& m) {
  size_t len = etl::min((size_t)m.data.size, _rx_buffer.size());
  etl::copy_n(m.data.bytes, len, _rx_buffer.begin());
  size_t tr = SPIService.transfer(etl::span<uint8_t>(_rx_buffer.data(), len));
  if (tr == 0) {
    emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }
  rpc_pb_SpiTransferResponse resp = rpc_pb_SpiTransferResponse_init_default;
  const size_t to_copy = etl::min(len, sizeof(resp.data.bytes));
  resp.data.size = (pb_size_t)to_copy;
  if (to_copy > 0) etl::copy_n(_rx_buffer.data(), to_copy, resp.data.bytes);
  if (!send(rpc::CommandId::CMD_SPI_TRANSFER_RESP, ctx.sequence_id, resp)) {
  }
}
#endif

void BridgeClass::_handleStatusMalformed(
    const bridge::router::CommandContext&) {
  enterSafeState();
}

void BridgeClass::_handleLinkSync(const bridge::router::CommandContext& ctx,
                                  const rpc_pb_LinkSync& m) {
  rpc_pb_LinkSync resp = rpc_pb_LinkSync_init_default;
  const size_t n_size =
      etl::min(static_cast<size_t>(m.nonce.size),
               static_cast<size_t>(rpc::RPC_HANDSHAKE_NONCE_LENGTH));
  etl::copy_n(m.nonce.bytes, n_size, resp.nonce.bytes);
  resp.nonce.size = static_cast<pb_size_t>(n_size);
  if (!_shared_secret.empty()) {
    etl::array<uint8_t, rpc::RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> out_tag;
    if (rpc::security::handshake_authenticate(
            etl::span<const uint8_t>(_shared_secret),
            etl::span<const uint8_t>(m.nonce.bytes, n_size),
            etl::span<const uint8_t>(m.tag.bytes, m.tag.size),
            etl::span<uint8_t>(out_tag))) {
      etl::copy_n(out_tag.data(), rpc::RPC_HANDSHAKE_TAG_LENGTH,
                  resp.tag.bytes);
      resp.tag.size = rpc::RPC_HANDSHAKE_TAG_LENGTH;
      rpc::security::derive_session_key(
          etl::span<const uint8_t>(_shared_secret),
          etl::span<const uint8_t>(m.nonce.bytes, n_size),
          etl::span<uint8_t>(_session_key));
      _tx_nonce_counter = 0;
      _rx_nonce_counter = 0;
      rpc::security::secure_zero(etl::span<uint8_t>(out_tag));
    } else {
      _fsm.receive(bridge::fsm::EvHandshakeFailed());
      emitStatus(rpc::StatusCode::STATUS_ERROR);
      return;
    }
  }
  _fsm.receive(bridge::fsm::EvHandshakeStart());
  _fsm.receive(bridge::fsm::EvHandshakeComplete());
  if (!send(rpc::CommandId::CMD_LINK_SYNC_RESP, ctx.sequence_id, resp)) {
  }
}

void BridgeClass::_handleLinkReset(const bridge::router::CommandContext& ctx) {
  if (ctx.envelope->which_payload_type != 0) {
    rpc_pb_HandshakeConfig res_msg = rpc_pb_HandshakeConfig_init_default;
    if (_decodePayload(ctx, rpc::Payload::get_fields<rpc_pb_HandshakeConfig>(),
                       &res_msg,
                       rpc::Payload::get_tag<rpc_pb_HandshakeConfig>(),
                       sizeof(rpc_pb_HandshakeConfig))) {
      _handleSetTiming(res_msg);
    }
  }
  _fsm.receive(bridge::fsm::EvReset());
  if (!sendFrame(rpc::CommandId::CMD_LINK_RESET_RESP, ctx.sequence_id)) {
  }
}

void BridgeClass::_handleGetCapabilities(
    const bridge::router::CommandContext& ctx) {
  rpc_pb_Capabilities resp = rpc_pb_Capabilities_init_default;
  bridge::hal::fillCapabilities(resp);
  if (!send(rpc::CommandId::CMD_GET_CAPABILITIES_RESP, ctx.sequence_id, resp)) {
  }
}

void BridgeClass::_handleXoff(const bridge::router::CommandContext&) {
  _tx_enabled = false;
}
void BridgeClass::_handleXon(const bridge::router::CommandContext&) {
  _tx_enabled = true;
}

void BridgeClass::_handleStatusAck(
    const bridge::router::CommandContext& /*ctx*/, const rpc_pb_AckPacket& m) {
  _handleAck(m.command_id);
}

void BridgeClass::_handleGetVersion(const bridge::router::CommandContext& ctx) {
  rpc_pb_VersionResponse resp = {};
  resp.major = rpc::FIRMWARE_VERSION_MAJOR;
  resp.minor = rpc::FIRMWARE_VERSION_MINOR;
  resp.patch = (uint32_t)rpc::FIRMWARE_VERSION_PATCH;
  if (!send(rpc::CommandId::CMD_GET_VERSION_RESP, ctx.sequence_id, resp)) {
  }
}

void BridgeClass::_handleGetFreeMemory(
    const bridge::router::CommandContext& ctx) {
  rpc_pb_FreeMemoryResponse resp = {};
  resp.value = (uint32_t)bridge::hal::getFreeMemory();
  if (!send(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, ctx.sequence_id, resp)) {
  }
}

void BridgeClass::_handleSetTiming(const rpc_pb_HandshakeConfig& msg) {
  _applyTimingConfig(msg);
}
void BridgeClass::_applyTimingConfig(const rpc_pb_HandshakeConfig& msg) {
  _ack_timeout_ms = (uint16_t)msg.ack_timeout_ms;
  _retry_limit = (uint8_t)msg.ack_retry_limit;
  _response_timeout_ms = msg.response_timeout_ms;
}

void BridgeClass::_handleReceivedFrame(etl::span<const uint8_t> p) {
  auto res = rpc::parse_frame(p);
  if (!res) {
    emitStatus(rpc::StatusCode::STATUS_MALFORMED);
    return;
  }
  rpc_pb_RpcEnvelope envelope = res.value();
  if (envelope.version != rpc::PROTOCOL_VERSION) {
    emitStatus(rpc::StatusCode::STATUS_MALFORMED);
    return;
  }
  const uint16_t raw_cmd = envelope.command_id;
  const bool is_excluded = rpc::is_system_command(raw_cmd);
  if (isSynchronized() && !_shared_secret.empty() && !is_excluded) {
    if (envelope.payload_type.encrypted_payload_with_tag.size < 16) {
      emitStatus(rpc::StatusCode::STATUS_ERROR);
      return;
    }
    const size_t ct_size =
        envelope.payload_type.encrypted_payload_with_tag.size - 16;
    etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> dec_pl;
    if (!rpc::security::aead_decrypt_frame(
            raw_cmd, envelope.sequence_id,
            etl::span<const uint8_t>(
                envelope.payload_type.encrypted_payload_with_tag.bytes,
                ct_size),
            etl::span<const uint8_t>(
                envelope.payload_type.encrypted_payload_with_tag.bytes +
                    ct_size,
                16),
            _session_key, etl::span<const uint8_t>(envelope.nonce.bytes, 12),
            dec_pl) ||
        !rpc::security::validate_frame_nonce(
            etl::span<const uint8_t>(envelope.nonce.bytes, 12),
            &_rx_nonce_counter)) {
      emitStatus(rpc::StatusCode::STATUS_ERROR);
      return;
    }
    etl::copy_n(dec_pl.data(), ct_size,
                envelope.payload_type.encrypted_payload_with_tag.bytes);
    envelope.payload_type.encrypted_payload_with_tag.size =
        static_cast<pb_size_t>(ct_size);
  }
  _dispatchCommand(envelope);
}

bool BridgeClass::_isSecurityCheckPassed(uint16_t cmd) const {
  if (_shared_secret.empty()) return true;
  if (rpc::is_system_command(cmd)) return true;
  return _fsm.isSynchronized();
}

void BridgeClass::signalXoff() {
  if (!sendFrame(rpc::CommandId::CMD_XOFF)) {
  }
}
void BridgeClass::signalXon() {
  if (!sendFrame(rpc::CommandId::CMD_XON)) {
  }
}

bool BridgeClass::_decodePayload(const bridge::router::CommandContext& ctx,
                                 const pb_msgdesc_t* fields, void* dest,
                                 pb_size_t expected_tag) {
  if (ctx.envelope->which_payload_type ==
      rpc_pb_RpcEnvelope_encrypted_payload_with_tag_tag) {
    pb_istream_t stream = pb_istream_from_buffer(
        ctx.envelope->payload_type.encrypted_payload_with_tag.bytes,
        ctx.envelope->payload_type.encrypted_payload_with_tag.size);
    return pb_decode_noinit(&stream, fields, dest);
  } else if (ctx.envelope->which_payload_type == expected_tag) {
    etl::copy_n(reinterpret_cast<const uint8_t*>(&ctx.envelope->payload_type),
                fields->msg_size, reinterpret_cast<uint8_t*>(dest));
    return true;
  }
  return false;
}

bool BridgeClass::_sendEncryptedHelper(uint16_t raw_cmd, uint16_t seq,
                                       const pb_msgdesc_t* fields,
                                       const void* packet) {
  if (is_reliable_cmd(raw_cmd)) {
    BRIDGE_ATOMIC_BLOCK {
      if (_pending_tx_queue.full()) return false;
      auto* buf = _tx_payload_pool.allocate();
      if (!buf) return false;
      pb_ostream_t out_stream =
          pb_ostream_from_buffer(buf->data.data(), buf->data.size());
      if (pb_encode(&out_stream, fields, packet)) {
        _pending_tx_queue.push_back(
            {raw_cmd, seq, buf, out_stream.bytes_written});
        if (!_fsm.isAwaitingAck()) _flushPendingTxQueue();
        return true;
      }
      _tx_payload_pool.release(buf);
      return false;
    }
  } else {
    pb_ostream_t out_stream =
        pb_ostream_from_buffer(_transient_buffer.data(), rpc::MAX_PAYLOAD_SIZE);
    if (pb_encode(&out_stream, fields, packet)) {
      _transmit(raw_cmd, seq,
                etl::span<const uint8_t>(_transient_buffer.data(),
                                         out_stream.bytes_written));
      return true;
    }
    return false;
  }
}

namespace bridge {
void SafeStatePolicy::handle(::BridgeClass& bridge, const etl::exception&) {
  bridge.enterSafeState();
}
}  // namespace bridge
