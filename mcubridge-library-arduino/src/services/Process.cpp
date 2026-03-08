#include "Bridge.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"

// [OPTIMIZATION] Numerical status codes used instead of PROGMEM strings.

#if BRIDGE_ENABLE_PROCESS

ProcessClass::ProcessClass()
    : etl::imessage_router(etl::imessage_router::MESSAGE_ROUTER) {
  reset();
}

void ProcessClass::receive(const etl::imessage& msg) {
  const uint16_t cmd = static_cast<uint16_t>(msg.get_message_id());
  if (cmd != rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP) &&
      cmd != rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP)) {
    return;
  }

  const auto& cmd_msg = static_cast<const bridge::router::CommandMessage&>(msg);

  if (cmd == rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP)) {
    Bridge._withPayload<rpc::payload::ProcessRunAsyncResponse>(
        cmd_msg, [this](const rpc::payload::ProcessRunAsyncResponse& pl) {
          if (_process_run_async_handler.is_valid()) {
            _process_run_async_handler(static_cast<int16_t>(pl.pid));
          }
        });
  } else if (cmd == rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP)) {
    Bridge._withPayload<rpc::payload::ProcessPollResponse>(
        cmd_msg, [this](const rpc::payload::ProcessPollResponse& pl) {
          if (_process_poll_handler.is_valid()) {
            _process_poll_handler(
                static_cast<rpc::StatusCode>(pl.status), pl.exit_code,
                etl::span<const uint8_t>(pl.stdout_data, pl.stdout_len),
                etl::span<const uint8_t>(pl.stderr_data, pl.stderr_len));
            _popPendingProcessPid();
          }
        });
  }
}

bool ProcessClass::accepts(etl::message_id_t id) const {
  const uint16_t cmd = static_cast<uint16_t>(id);
  return cmd == rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP) ||
         cmd == rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP);
}

void ProcessClass::reset() { _pending_process_pids.clear(); }

void ProcessClass::runAsync(etl::string_view command) {
  (void)Bridge.sendStringCommand(rpc::CommandId::CMD_PROCESS_RUN_ASYNC, command,
                                rpc::MAX_PAYLOAD_SIZE - 1);
}

void ProcessClass::poll(int16_t pid) {
  if (pid < 0) return;

  const uint16_t pid_u16 = static_cast<uint16_t>(pid);
  if (!_pushPendingProcessPid(pid_u16)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
    return;
  }

  if (!Bridge.sendValue(rpc::CommandId::CMD_PROCESS_POLL, pid_u16)) {
    _popPendingProcessPid();  // Cleanup if failed to send
  }
}

void ProcessClass::kill(int16_t pid) {
  if (pid < 0) return;
  (void)Bridge.sendValue(rpc::CommandId::CMD_PROCESS_KILL, static_cast<uint16_t>(pid));
}

bool ProcessClass::_pushPendingProcessPid(uint16_t pid) {
  if (_pending_process_pids.full()) {
    return false;
  }
  _pending_process_pids.push(pid);
  return true;
}

uint16_t ProcessClass::_popPendingProcessPid() {
  if (_pending_process_pids.empty()) {
    return rpc::RPC_INVALID_ID_SENTINEL;
  }

  uint16_t pid = _pending_process_pids.front();
  _pending_process_pids.pop();
  return pid;
}

#endif
#if BRIDGE_ENABLE_PROCESS
ProcessClass Process;
#endif
