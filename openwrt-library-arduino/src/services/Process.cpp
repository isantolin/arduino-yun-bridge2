#include "Bridge.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"

// [OPTIMIZATION] Numerical status codes used instead of PROGMEM strings.

#if BRIDGE_ENABLE_PROCESS

ProcessClass::ProcessClass() 
  : _pending_process_pids(), // Auto-initialized by ETL
    _process_run_handler(),
    _process_poll_handler(),
    _process_run_async_handler() {
}

void ProcessClass::run(etl::string_view command) {
  if (command.empty()) return;
  if (!Bridge.sendStringCommand(rpc::CommandId::CMD_PROCESS_RUN, 
                               command, rpc::MAX_PAYLOAD_SIZE - 1)) {
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, F("Command too long"));
  }
}

void ProcessClass::runAsync(etl::string_view command) {
  if (command.empty()) return;
  if (!Bridge.sendStringCommand(rpc::CommandId::CMD_PROCESS_RUN_ASYNC, 
                               command, rpc::MAX_PAYLOAD_SIZE - 1)) {
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, F("Command too long"));
  }
}

// Helper: build a 2-byte PID payload and send a single frame.
static void sendPidCommand(rpc::CommandId command, uint16_t pid_u16) {
  etl::array<uint8_t, 2> pid_payload;
  rpc::write_u16_be(pid_payload.data(), pid_u16);
  (void)Bridge.sendFrame(command, pid_payload.data(), pid_payload.size());
}

void ProcessClass::poll(int16_t pid) {
  if (pid < 0) {
    return;
  }

  const uint16_t pid_u16 = static_cast<uint16_t>(pid);
  if (!_pushPendingProcessPid(pid_u16)) {
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, (const char*)nullptr);
    return;
  }

  sendPidCommand(rpc::CommandId::CMD_PROCESS_POLL, pid_u16);
}

void ProcessClass::kill(int16_t pid) {
  sendPidCommand(rpc::CommandId::CMD_PROCESS_KILL, static_cast<uint16_t>(pid));
}

void ProcessClass::handleResponse(const rpc::Frame& frame) {
  const rpc::CommandId command = static_cast<rpc::CommandId>(frame.header.command_id);
  const size_t payload_length = frame.header.payload_length;
  const uint8_t* payload_data = frame.payload.data();

  if (payload_length == 0 || !payload_data) return;

  switch (command) {
    case rpc::CommandId::CMD_PROCESS_RUN_RESP:
      if (_process_run_handler.is_valid()) {
        auto msg = rpc::payload::ProcessRunResponse::parse(payload_data);
        _process_run_handler(static_cast<rpc::StatusCode>(msg.status), msg.stdout_data, msg.stdout_len, msg.stderr_data, msg.stderr_len);
      }
      break;
    case rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP:
      if (_process_run_async_handler.is_valid() && payload_length >= rpc::payload::ProcessRunAsyncResponse::SIZE) {
        auto msg = rpc::payload::ProcessRunAsyncResponse::parse(payload_data);
        _process_run_async_handler(static_cast<int>(msg.pid));
      }
      break;
    case rpc::CommandId::CMD_PROCESS_POLL_RESP:
      if (_process_poll_handler.is_valid()) {
        auto msg = rpc::payload::ProcessPollResponse::parse(payload_data);
        _process_poll_handler(static_cast<rpc::StatusCode>(msg.status), msg.exit_code, msg.stdout_data, msg.stdout_len, msg.stderr_data, msg.stderr_len);
        _popPendingProcessPid(); 
      }
      break;
    default:
      break;
  }
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