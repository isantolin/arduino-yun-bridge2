#ifndef BRIDGE_EVENTS_H
#define BRIDGE_EVENTS_H

#include <etl/observer.h>

#include "rpc_protocol.h"

// [SIL-2] Observer Event Types
struct MsgBridgeSynchronized {};
struct MsgBridgeLost {};

// [SIL-2] Observer Interface for System Events
struct BridgeObserver : public etl::observer<MsgBridgeSynchronized,
                                             MsgBridgeLost> {
  virtual ~BridgeObserver() = default; // GCOVR_EXCL_LINE — compiler-generated destructor
  virtual void notification(MsgBridgeSynchronized) {}
  virtual void notification(MsgBridgeLost) {}
};

#endif
