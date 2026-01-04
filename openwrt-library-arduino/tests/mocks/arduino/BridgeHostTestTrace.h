#ifndef BRIDGE_HOST_TEST_TRACE_H
#define BRIDGE_HOST_TEST_TRACE_H

#include <stddef.h>
#include <stdint.h>

#if defined(BRIDGE_HOST_TEST)
  #include <unistd.h>

namespace bridge_host_test {

inline void write_all(const char* data, size_t len) {
  while (len > 0) {
    const ssize_t written = ::write(2, data, len);
    if (written <= 0) {
      return;
    }
    data += static_cast<size_t>(written);
    len -= static_cast<size_t>(written);
  }
}

inline void write_literal(const char* s) {
  const char* p = s;
  while (*p != '\0') {
    ++p;
  }
  write_all(s, static_cast<size_t>(p - s));
}

inline void write_u32_decimal(unsigned int value) {
  char buf[16];
  size_t i = 0;
  do {
    const unsigned int digit = value % 10U;
    buf[i++] = static_cast<char>('0' + digit);
    value /= 10U;
  } while (value != 0U && i < sizeof(buf));

  while (i > 0) {
    --i;
    write_all(&buf[i], 1);
  }
}

inline void send_frame(uint16_t command_id, bool awaiting_ack, uint8_t pending_count) {
  write_literal("[Bridge] _sendFrame ID=");
  write_u32_decimal(command_id);
  write_literal(" AwaitingAck=");
  write_u32_decimal(awaiting_ack ? 1U : 0U);
  write_literal(" PendingCount=");
  write_u32_decimal(static_cast<unsigned int>(pending_count));
  write_all("\n", 1);
}

inline void send_frame_no_ack(uint16_t command_id) {
  write_literal("[Bridge] _sendFrame: No ACK required for ID=");
  write_u32_decimal(command_id);
  write_literal(", sending immediate\n");
}

inline void send_frame_queued(uint16_t command_id) {
  write_literal("[Bridge] _sendFrame: Awaiting ACK, enqueuing ID=");
  write_u32_decimal(command_id);
  write_all("\n", 1);
}

inline void send_frame_immediate(uint16_t command_id) {
  write_literal("[Bridge] _sendFrameImmediate: ID=");
  write_u32_decimal(command_id);
  write_literal(" sent, set _awaiting_ack=true\n");
}

}  // namespace bridge_host_test

#else

namespace bridge_host_test {

inline void send_frame(uint16_t, bool, uint8_t) {}
inline void send_frame_no_ack(uint16_t) {}
inline void send_frame_queued(uint16_t) {}
inline void send_frame_immediate(uint16_t) {}

}  // namespace bridge_host_test

#endif

#endif  // BRIDGE_HOST_TEST_TRACE_H
