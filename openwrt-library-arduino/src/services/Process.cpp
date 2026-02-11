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
    _process_run_handler(),
    _process_poll_handler(),
    _process_run_async_handler() {
}

void ProcessClass::run(const char* command) {
  if (!command || *command == '\0') return;
  if (!Bridge.sendStringCommand(rpc::CommandId::CMD_PROCESS_RUN, 
                               command, rpc::MAX_PAYLOAD_SIZE - 1)) {
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, F("Command too long"));
  }
}

void ProcessClass::runAsync(const char* command) {
  if (!command || *command == '\0') return;
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

struct ProcessResponse {
  rpc::StatusCode status;
  uint8_t running;
  const uint8_t* stdout_ptr;
  uint16_t stdout_len;
  const uint8_t* stderr_ptr;
  uint16_t stderr_len;

  static bool parse(const uint8_t* data, size_t len, size_t header_size, ProcessResponse& out) {
    if (len < header_size + kLenFieldSize) return false;
    out.status = static_cast<rpc::StatusCode>(data[0]);
    out.running = (header_size > kRunRespHeaderSize) ? data[1] : 0;
    out.stdout_len = rpc::read_u16_be(data + header_size - kLenFieldSize);
    if (len < header_size + out.stdout_len + kLenFieldSize) return false;
    out.stdout_ptr = data + header_size;
    out.stderr_len = rpc::read_u16_be(data + header_size + out.stdout_len);
    if (len < header_size + out.stdout_len + kLenFieldSize + out.stderr_len) return false;
    out.stderr_ptr = data + header_size + out.stdout_len + kLenFieldSize;
    return true;
  }
};

void ProcessClass::handleResponse(const rpc::Frame& frame) {
  const rpc::CommandId command = static_cast<rpc::CommandId>(frame.header.command_id);
  const size_t payload_length = frame.header.payload_length;
  const uint8_t* payload_data = frame.payload.data();

  if (payload_length == 0) return;

  ProcessResponse res{};
  switch (command) {
    case rpc::CommandId::CMD_PROCESS_RUN_RESP:
      if (_process_run_handler && ProcessResponse::parse(payload_data, payload_length, kRunRespHeaderSize, res)) {
        _process_run_handler(res.status, res.stdout_ptr, res.stdout_len, res.stderr_ptr, res.stderr_len);
      }
      break;
    case rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP:
      if (_process_run_async_handler && payload_length >= 2) {
        _process_run_async_handler(static_cast<int>(rpc::read_u16_be(payload_data)));
      }
      break;
    case rpc::CommandId::CMD_PROCESS_POLL_RESP:
      if (_process_poll_handler && ProcessResponse::parse(payload_data, payload_length, kPollRespHeaderSize, res)) {
        _process_poll_handler(res.status, res.running, res.stdout_ptr, res.stdout_len, res.stderr_ptr, res.stderr_len);
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