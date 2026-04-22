#include "rle.h"

#include <etl/algorithm.h>
#include <etl/fsm.h>
#include <etl/iterator.h>
#include <etl/message.h>

namespace rle {
namespace {

enum StateId { LITERAL = 0, ESC_MARKER, ESC_VAL };

struct ByteMsg : public etl::message<1> {
  uint8_t b;
  explicit ByteMsg(uint8_t byte) : b(byte) {}
};

class RleFsm;

struct LiteralState
    : public etl::fsm_state<RleFsm, LiteralState, StateId::LITERAL, ByteMsg> {
  etl::fsm_state_id_t on_event(const ByteMsg& msg);
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return get_state_id();
  }
};

struct EscMarkerState : public etl::fsm_state<RleFsm, EscMarkerState,
                                              StateId::ESC_MARKER, ByteMsg> {
  etl::fsm_state_id_t on_event(const ByteMsg& msg);
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return get_state_id();
  }
};

struct EscValState
    : public etl::fsm_state<RleFsm, EscValState, StateId::ESC_VAL, ByteMsg> {
  etl::fsm_state_id_t on_event(const ByteMsg& msg);
  etl::fsm_state_id_t on_event_unknown(const etl::imessage&) {
    return get_state_id();
  }
};

class RleFsm : public etl::fsm {
 public:
  etl::span<uint8_t>::iterator it;
  etl::span<uint8_t>::iterator end;
  uint8_t esc_count = 0;
  bool error = false;

  LiteralState s_literal;
  EscMarkerState s_marker;
  EscValState s_val;
  etl::array<etl::ifsm_state*, 3> state_list;

  explicit RleFsm(etl::span<uint8_t> dst)
      : etl::fsm(StateId::LITERAL), it(dst.begin()), end(dst.end()) {
    state_list[0] = &s_literal;
    state_list[1] = &s_marker;
    state_list[2] = &s_val;
    set_states(state_list.data(), state_list.size());
    start();
  }
};

etl::fsm_state_id_t LiteralState::on_event(const ByteMsg& msg) {
  auto& m = get_fsm_context();
  if (msg.b == ESCAPE_BYTE) return StateId::ESC_MARKER;
  if (m.it == m.end) {
    m.error = true;
    return StateId::LITERAL;
  }
  *m.it++ = msg.b;
  return StateId::LITERAL;
}

etl::fsm_state_id_t EscMarkerState::on_event(const ByteMsg& msg) {
  auto& m = get_fsm_context();
  m.esc_count = msg.b;
  return StateId::ESC_VAL;
}

etl::fsm_state_id_t EscValState::on_event(const ByteMsg& msg) {
  auto& m = get_fsm_context();
  size_t run_len = (m.esc_count == SINGLE_ESCAPE_MARKER)
                       ? 1
                       : static_cast<size_t>(m.esc_count) + rpc::RPC_RLE_OFFSET;
  if (static_cast<size_t>(etl::distance(m.it, m.end)) < run_len) {
    m.error = true;
    return StateId::LITERAL;
  }
  etl::fill_n(m.it, run_len, msg.b);
  m.it += run_len;
  return StateId::LITERAL;
}

}  // namespace

size_t decode(etl::span<const uint8_t> src, etl::span<uint8_t> dst) {
  if (src.empty() || dst.empty()) return 0;
  RleFsm fsm(dst);

  etl::for_each(src.begin(), src.end(), [&fsm](uint8_t b) {
    if (!fsm.error) {
      ByteMsg msg(b);
      fsm.receive(msg);
    }
  });

  if (fsm.error || fsm.get_state_id() != StateId::LITERAL) return 0;
  return static_cast<size_t>(etl::distance(dst.begin(), fsm.it));
}

}  // namespace rle
