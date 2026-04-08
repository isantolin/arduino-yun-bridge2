#ifndef PROTOCOL_BRIDGE_EVENTS_H
#define PROTOCOL_BRIDGE_EVENTS_H

#include <etl/observer.h>
#include <etl/message.h>

// [SIL-2] Global Notification Events
struct MsgBridgeSynchronized : public etl::message<0> {};
struct MsgBridgeLost : public etl::message<1> {};

// Bridge Observer Interface
using BridgeObserver = etl::observer<MsgBridgeSynchronized, MsgBridgeLost>;

#endif
