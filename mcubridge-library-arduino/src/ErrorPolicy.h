#ifndef BRIDGE_ERROR_POLICY_H
#define BRIDGE_ERROR_POLICY_H

#include <Arduino.h>
#include <etl/exception.h>

template <typename TStream> class BridgeClass;  // Forward declaration (global)

namespace bridge {

/**
 * [SIL-2] SafeStatePolicy: Defines behavior for fatal system errors.
 * Modernized to provide deterministic recovery or safe-state entry.
 */
class SafeStatePolicy {
 public:
  // Defined in Bridge.cpp to avoid circular dependency
  #if defined(BRIDGE_HOST_TEST)
#else
  static void handle(BridgeClass<HardwareSerial>& bridge, const etl::exception& e);
#endif
};

}  // namespace bridge

#endif
