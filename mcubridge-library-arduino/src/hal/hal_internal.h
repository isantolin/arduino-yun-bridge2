#pragma once

namespace bridge::hal::internal {

// Production implementation shared by the default HAL entry point and
// host-only test overrides.
bool run_power_on_self_tests_impl();

}  // namespace bridge::hal::internal
