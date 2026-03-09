#include "services/Process.h"

#include "Bridge.h"
#include "protocol/rpc_protocol.h"

#if BRIDGE_ENABLE_PROCESS

ProcessClass::ProcessClass() = default;

void ProcessClass::reset() { _pending_process_pids.clear(); }

void ProcessClass::runAsync(etl::string_view command) {
  (void)Bridge.sendStringCommand(rpc::CommandId::CMD_PROCESS_RUN_ASYNC, command,
                                rpc::RPC_MAX_PROCESS_COMMAND_LENGTH);
}

void ProcessClass::poll(int16_t pid) {
  uint16_t pid_u16 = static_cast<uint16_t>(pid);
  if (!_pushPendingProcessPid(pid_u16)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
    return;
  }

  if (!Bridge.sendValue(rpc::CommandId::CMD_PROCESS_POLL, pid_u16)) {
    (void)_popPendingProcessPid();  // Clean up if failed to send
  }
}

void ProcessClass::kill(int16_t pid) {
  (void)Bridge.sendValue(rpc::CommandId::CMD_PROCESS_KILL,
                        static_cast<uint16_t>(pid));
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
  return etl::optional<uint16_t>(pid);
}

#endif
