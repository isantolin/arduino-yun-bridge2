#include "Bridge.h"
#include "protocol/rpc_protocol.h"

using namespace rpc;

ProcessClass::ProcessClass() {}

void ProcessClass::kill(int pid) {
  uint8_t pid_payload[2];
  write_u16_be(pid_payload, static_cast<uint16_t>(pid));
  (void)Bridge.sendFrame(CommandId::CMD_PROCESS_KILL, pid_payload, 2);
}
