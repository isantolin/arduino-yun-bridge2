#include "Bridge.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"

// [OPTIMIZATION] Numerical status codes used instead of PROGMEM strings.

#if BRIDGE_ENABLE_PROCESS

ProcessClass::ProcessClass() { reset(); }

void ProcessClass::reset() { _pending_process_pids.clear(); }

void ProcessClass::runAsync(etl::string_view command) {
  if (command.empty()) return;
  if (!Bridge.sendStringCommand(rpc::CommandId::CMD_PROCESS_RUN_ASYNC, command,
                                rpc::MAX_PAYLOAD_SIZE - 1)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
  }
}

void ProcessClass::poll(int16_t pid) {
  if (pid < 0) {
    return;
  }

  const uint16_t pid_u16 = static_cast<uint16_t>(pid);
  if (!_pushPendingProcessPid(pid_u16)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
    return;
  }

  auto sendPid = [](rpc::CommandId command, uint16_t pid_val) {
    etl::array<uint8_t, 2> pid_payload;
    rpc::write_u16_be(pid_payload.data(), pid_val);
    return Bridge.sendFrame(command, etl::span<const uint8_t>(pid_payload.data(),
                                                             pid_payload.size()));
  };

  if (!sendPid(rpc::CommandId::CMD_PROCESS_POLL, pid_u16)) {
    _popPendingProcessPid();  // Cleanup if failed to send
  }
}

void ProcessClass::kill(int16_t pid) {
  if (pid < 0) return;
  etl::array<uint8_t, 2> pid_payload;
  rpc::write_u16_be(pid_payload.data(), static_cast<uint16_t>(pid));
  (void)Bridge.sendFrame(rpc::CommandId::CMD_PROCESS_KILL,
                         etl::span<const uint8_t>(pid_payload.data(),
                                                  pid_payload.size()));
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