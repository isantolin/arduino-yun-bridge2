#include "Bridge.h"
#include "protocol/rpc_protocol.h"

// [OPTIMIZATION] Numerical status codes used instead of PROGMEM strings.

// Response field sizes (bytes) for CMD_PROCESS_RUN_RESP / CMD_PROCESS_POLL_RESP
static constexpr size_t kStatusFieldSize = 1;
static constexpr size_t kLenFieldSize = 2;
// CMD_PROCESS_RUN_RESP header: status(1) + stdout_len(2)
static constexpr size_t kRunRespHeaderSize = kStatusFieldSize + kLenFieldSize;
// CMD_PROCESS_POLL_RESP header: status(1) + running(1) + stdout_len(2)
static constexpr size_t kPollRespHeaderSize = kStatusFieldSize + 1 + kLenFieldSize;

ProcessClass::ProcessClass() 
  : _pending_process_pids(), // Auto-initialized by ETL
    _process_run_handler(nullptr),
    _process_poll_handler(nullptr),
    _process_run_async_handler(nullptr) {
}

void ProcessClass::run(const char* command) {
  if (!command || !*command) {
    return;
  }
  size_t len = strlen(command);
  if (len > rpc::MAX_PAYLOAD_SIZE) {
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, (const char*)nullptr);
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
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, (const char*)nullptr);
    return;
  }
  (void)Bridge.sendFrame(
      rpc::CommandId::CMD_PROCESS_RUN_ASYNC,
      reinterpret_cast<const uint8_t*>(command),
      len);
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

  if (payload_length == 0) return;

  switch (command) {
    case rpc::CommandId::CMD_PROCESS_RUN_RESP:
      if (_process_run_handler && payload_length >= kRunRespHeaderSize + kLenFieldSize) {
        rpc::StatusCode status = static_cast<rpc::StatusCode>(payload_data[0]);
        uint16_t stdout_len = rpc::read_u16_be(payload_data + kStatusFieldSize);
        
        // Safety check: payload must contain at least (status + stdout_len_header + stdout + stderr_len_header)
        if (payload_length >= static_cast<size_t>(kRunRespHeaderSize + stdout_len + kLenFieldSize)) {
            const uint8_t* stdout_ptr = payload_data + kRunRespHeaderSize;
            uint16_t stderr_len = rpc::read_u16_be(payload_data + kRunRespHeaderSize + stdout_len);
            
            // Final safety check: total payload must accommodate stderr
            if (payload_length >= static_cast<size_t>(kRunRespHeaderSize + stdout_len + kLenFieldSize + stderr_len)) {
                const uint8_t* stderr_ptr = payload_data + kRunRespHeaderSize + stdout_len + kLenFieldSize;
                _process_run_handler(status, stdout_ptr, stdout_len, stderr_ptr, stderr_len);
            }
        }
      }
      break;
    case rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP:
      if (_process_run_async_handler && payload_length >= 2) {
        uint16_t pid = rpc::read_u16_be(payload_data);
        _process_run_async_handler(static_cast<int>(pid));
      }
      break;
    case rpc::CommandId::CMD_PROCESS_POLL_RESP:
      if (_process_poll_handler && payload_length >= kPollRespHeaderSize + kLenFieldSize) {
        rpc::StatusCode status = static_cast<rpc::StatusCode>(payload_data[0]);
        uint8_t running = payload_data[1];
        
        _popPendingProcessPid(); 
        
        uint16_t stdout_len = rpc::read_u16_be(payload_data + kStatusFieldSize + 1);
        if (payload_length >= static_cast<size_t>(kPollRespHeaderSize + stdout_len + kLenFieldSize)) {
             const uint8_t* stdout_ptr = payload_data + kPollRespHeaderSize;
             uint16_t stderr_len = rpc::read_u16_be(payload_data + kPollRespHeaderSize + stdout_len);
             
             if (payload_length >= static_cast<size_t>(kPollRespHeaderSize + stdout_len + kLenFieldSize + stderr_len)) {
                 const uint8_t* stderr_ptr = payload_data + kPollRespHeaderSize + stdout_len + kLenFieldSize;
                 _process_poll_handler(status, running, stdout_ptr, stdout_len, stderr_ptr, stderr_len);
             }
        }
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