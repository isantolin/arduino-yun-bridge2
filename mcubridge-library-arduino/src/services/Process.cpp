#include "Bridge.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"

// [OPTIMIZATION] Numerical status codes used instead of PROGMEM strings.

#if BRIDGE_ENABLE_PROCESS

ProcessClass::ProcessClass() { reset(); }

void ProcessClass::reset() { _pending_process_pids.clear(); }

void ProcessClass::runAsync(etl::string_view command) {
  rpc::payload::ProcessRunAsync msg = {};
  const size_t len = etl::min(command.length(), sizeof(msg.command) - 1);
  etl::copy_n(command.data(), len, msg.command);
  msg.command[len] = '\0';
  static_cast<void>(Bridge.sendPbFrame(rpc::CommandId::CMD_PROCESS_RUN_ASYNC, msg));
}

void ProcessClass::poll(int16_t pid) {
  if (pid < 0) return;

  const uint16_t pid_u16 = static_cast<uint16_t>(pid);
  if (!_pushPendingProcessPid(pid_u16)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
    return;
  }

  rpc::payload::ProcessPoll msg = {};
  msg.pid = pid_u16;
  if (!Bridge.sendPbFrame(rpc::CommandId::CMD_PROCESS_POLL, msg)) {
    _popPendingProcessPid();  // Cleanup if failed to send
  }
}

void ProcessClass::kill(int16_t pid) {
  if (pid < 0) return;
  rpc::payload::ProcessKill msg = {};
  msg.pid = static_cast<uint16_t>(pid);
  static_cast<void>(Bridge.sendPbFrame(rpc::CommandId::CMD_PROCESS_KILL, msg));
}

bool ProcessClass::_pushPendingProcessPid(uint16_t pid) {
  if (_pending_process_pids.full()) {
    return false;
  }
  _pending_process_pids.push(pid);
  return true;
}

etl::optional<uint16_t> ProcessClass::_popPendingProcessPid() {
  if (_pending_process_pids.empty()) {
    return etl::nullopt;
  }

  uint16_t pid = _pending_process_pids.front();
  _pending_process_pids.pop();
  return etl::make_optional(pid);
}

#endif