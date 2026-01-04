#include "Bridge.h"
#include "protocol/rpc_protocol.h"

ProcessClass::ProcessClass() 
  : _pending_process_poll_head(0),
    _pending_process_poll_count(0),
    _process_run_handler(nullptr),
    _process_poll_handler(nullptr),
    _process_run_async_handler(nullptr) {
  memset(_pending_process_pids, 0, sizeof(_pending_process_pids));
}

void ProcessClass::run(const char* command) {
  if (!command || !*command) {
    return;
  }
  size_t len = strlen(command);
  if (len > rpc::MAX_PAYLOAD_SIZE) {
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, F("process_run_payload_too_large"));
    return;
  }
  (void)Bridge.sendFrame(
      rpc::CommandId::CMD_PROCESS_RUN,
      reinterpret_cast<const uint8_t*>(command),
      len);
}

void ProcessClass::runAsync(const char* command) {
  if (!command || !*command) {
    return;
  }
  size_t len = strlen(command);
  if (len > rpc::MAX_PAYLOAD_SIZE) {
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, F("process_run_async_payload_too_large"));
    return;
  }
  (void)Bridge.sendFrame(
      rpc::CommandId::CMD_PROCESS_RUN_ASYNC,
      reinterpret_cast<const uint8_t*>(command),
      len);
}

void ProcessClass::poll(int pid) {
  if (pid < 0) {
    return;
  }

  const uint16_t pid_u16 = static_cast<uint16_t>(pid);
  if (!_pushPendingProcessPid(pid_u16)) {
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, F("process_poll_queue_full"));
    return;
  }

  uint8_t pid_payload[2];
  rpc::write_u16_be(pid_payload, pid_u16);
  (void)Bridge.sendFrame(rpc::CommandId::CMD_PROCESS_POLL, pid_payload, 2);
}

void ProcessClass::kill(int pid) {
  uint8_t pid_payload[2];
  rpc::write_u16_be(pid_payload, static_cast<uint16_t>(pid));
  (void)Bridge.sendFrame(rpc::CommandId::CMD_PROCESS_KILL, pid_payload, 2);
}

void ProcessClass::handleResponse(const rpc::Frame& frame) {
  const rpc::CommandId command = static_cast<rpc::CommandId>(frame.header.command_id);
  const size_t payload_length = frame.header.payload_length;
  const uint8_t* payload_data = frame.payload;

  switch (command) {
    case rpc::CommandId::CMD_PROCESS_RUN_RESP:
      if (_process_run_handler && payload_length >= 1 && payload_data) {
        rpc::StatusCode status = static_cast<rpc::StatusCode>(payload_data[0]);
        if (payload_length >= 5) {
            uint16_t stdout_len = rpc::read_u16_be(payload_data + 1);
            const uint8_t* stdout_ptr = payload_data + 3;
            if (payload_length >= static_cast<size_t>(3 + stdout_len + 2)) {
                uint16_t stderr_len = rpc::read_u16_be(payload_data + 3 + stdout_len);
                const uint8_t* stderr_ptr = payload_data + 3 + stdout_len + 2;
                _process_run_handler(status, stdout_ptr, stdout_len, stderr_ptr, stderr_len);
            }
        }
      }
      break;
    case rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP:
      if (_process_run_async_handler && payload_length >= 2 && payload_data) {
        uint16_t pid = rpc::read_u16_be(payload_data);
        _process_run_async_handler(static_cast<int>(pid));
      }
      break;
    case rpc::CommandId::CMD_PROCESS_POLL_RESP:
      if (_process_poll_handler && payload_length >= 2 && payload_data) {
        rpc::StatusCode status = static_cast<rpc::StatusCode>(payload_data[0]);
        uint8_t running = payload_data[1];
        
        _popPendingProcessPid(); 
        
        if (payload_length >= 6) {
             uint16_t stdout_len = rpc::read_u16_be(payload_data + 2);
             const uint8_t* stdout_ptr = payload_data + 4;
             if (payload_length >= static_cast<size_t>(4 + stdout_len + 2)) {
                 uint16_t stderr_len = rpc::read_u16_be(payload_data + 4 + stdout_len);
                 const uint8_t* stderr_ptr = payload_data + 4 + stdout_len + 2;
                 _process_poll_handler(status, running, stdout_ptr, stdout_len, stderr_ptr, stderr_len);
             }
        }
      }
      break;
    default:
      break;
  }
}

void ProcessClass::onProcessRunResponse(ProcessRunHandler handler) { _process_run_handler = handler; }
void ProcessClass::onProcessPollResponse(ProcessPollHandler handler) { _process_poll_handler = handler; }
void ProcessClass::onProcessRunAsyncResponse(ProcessRunAsyncHandler handler) { _process_run_async_handler = handler; }

bool ProcessClass::_pushPendingProcessPid(uint16_t pid) {
  if (_pending_process_poll_count >= BRIDGE_MAX_PENDING_PROCESS_POLLS) {
    return false;
  }

  uint8_t slot =
      (_pending_process_poll_head + _pending_process_poll_count) %
      BRIDGE_MAX_PENDING_PROCESS_POLLS;
  _pending_process_pids[slot] = pid;
  _pending_process_poll_count++;
  return true;
}

uint16_t ProcessClass::_popPendingProcessPid() {
  if (_pending_process_poll_count == 0) {
    return rpc::RPC_INVALID_ID_SENTINEL;
  }

  uint8_t slot = _pending_process_poll_head;
  uint16_t pid = _pending_process_pids[slot];
  _pending_process_poll_head =
      (_pending_process_poll_head + 1) % BRIDGE_MAX_PENDING_PROCESS_POLLS;
  _pending_process_poll_count--;
  _pending_process_pids[slot] = 0;
  return pid;
}
