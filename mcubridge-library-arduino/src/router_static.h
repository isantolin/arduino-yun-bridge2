#ifndef BRIDGE_ROUTER_STATIC_H
#define BRIDGE_ROUTER_STATIC_H

#include <stdint.h>
#include "Bridge.h"
#include "protocol/rpc_protocol.h"

namespace bridge {
namespace router {

template <uint16_t ID, auto Handler>
struct Route {
  static constexpr uint16_t id = ID;
  static constexpr auto handler = Handler;
};

template <typename... Routes>
class StaticRouter {
 public:
  static bool dispatch(BridgeClass& bridge,
                       const bridge::router::CommandContext& ctx) {
    return (try_route<Routes>(bridge, ctx) || ...);
  }

 private:
  template <typename R>
  static bool try_route(BridgeClass& bridge,
                        const bridge::router::CommandContext& ctx) {
    if (ctx.raw_command == R::id) {
      (bridge.*(R::handler))(ctx);
      return true;
    }
    return false;
  }
};

}  // namespace router
}  // namespace bridge

#endif
