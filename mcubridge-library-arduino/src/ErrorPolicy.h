#ifndef BRIDGE_ERROR_POLICY_H
#define BRIDGE_ERROR_POLICY_H

#include <Arduino.h>
#include <etl/exception.h>

class BridgeClass;  // Forward declaration (global)

namespace bridge {

/**
 * [SIL-2] SafeStatePolicy: Defines behavior for fatal system errors.
 * Modernized to provide deterministic recovery or safe-state entry.
 */
class SafeStatePolicy {
 public:
  // Defined in Bridge.cpp to avoid circular dependency
  static void handle(::BridgeClass& bridge, const etl::exception& e);

  void onFatalError() {
    // [SIL-2] Deterministic safe-state entry on fatal hardware/logic failure
    ::pinMode(13, OUTPUT);
    ::digitalWrite(13, HIGH);
  }
};

}  // namespace bridge

#endif
