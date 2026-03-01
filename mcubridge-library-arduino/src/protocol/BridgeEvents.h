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

// [SIL-2] Observer Interface for System Events
struct BridgeObserver : public etl::observer<MsgBridgeSynchronized,
                                             MsgBridgeLost, MsgBridgeError> {
  virtual ~BridgeObserver() = default;
  virtual void notification(MsgBridgeSynchronized) {}
  virtual void notification(MsgBridgeLost) {}
  virtual void notification(MsgBridgeError) {}
};

#endif
