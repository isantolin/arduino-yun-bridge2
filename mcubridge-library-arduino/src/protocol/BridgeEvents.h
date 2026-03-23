#ifndef BRIDGE_EVENTS_H
#define BRIDGE_EVENTS_H

#include <etl/observer.h>

#include "rpc_protocol.h"

// [SIL-2] Observer Event Types
struct MsgBridgeSynchronized {};
struct MsgBridgeLost {};
struct MsgBridgeError {
  rpc::StatusCode code;
};

struct MsgBridgeCommand {
  uint16_t command_id;
  uint16_t sequence_id;
  etl::span<const uint8_t> payload;
};

// [SIL-2] Observer Interface for System Events
struct BridgeObserver : public etl::observer<MsgBridgeSynchronized,
                                             MsgBridgeLost, MsgBridgeError, MsgBridgeCommand> {
  virtual ~BridgeObserver() = default;
  virtual void notification(MsgBridgeSynchronized) {}
  virtual void notification(MsgBridgeLost) {}
  virtual void notification(MsgBridgeError) {}
  virtual void notification(MsgBridgeCommand) {}
};

#endif
