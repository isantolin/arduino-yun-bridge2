#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "protocol/rpc_structs.h"
#include "test_support.h"

// Global stubs
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;
void setUp(void) {}
void tearDown(void) {}

template <typename T>
void test_roundtrip(const T& p) {
  uint8_t buffer[rpc::MAX_PAYLOAD_SIZE];
  pb_ostream_t ostream = pb_ostream_from_buffer(buffer, rpc::MAX_PAYLOAD_SIZE);
  TEST_ASSERT(p.encode(&ostream));
  size_t used = ostream.bytes_written;

  T p2 = {};
  pb_istream_t istream = pb_istream_from_buffer(buffer, used);
  TEST_ASSERT(p2.decode(&istream));
}

template <typename T>
void test_chaos_decode() {
  uint8_t buffer[2] = {0x91, 0xFF};  // Junk
  T p = {};
  pb_istream_t istream = pb_istream_from_buffer(buffer, 2);
  (void)p.decode(&istream);  // Should fail gracefully
}

void test_all_structs_roundtrip() {
  test_roundtrip([]() {
    rpc::payload::VersionResponse p;
    p.pb_msg.major = 1;
    p.pb_msg.minor = 2;
    p.pb_msg.patch = 3;
    return p;
  }());
  test_roundtrip([]() {
    rpc::payload::FreeMemoryResponse p;
    p.pb_msg.value = 1024;
    return p;
  }());
  test_roundtrip([]() {
    rpc::payload::Capabilities p;
    p.pb_msg.ver = 1;
    p.pb_msg.arch = 2;
    p.pb_msg.dig = 20;
    p.pb_msg.ana = 6;
    p.pb_msg.feat = 0xFF;
    return p;
  }());
  test_roundtrip([]() {
    rpc::payload::PinMode p;
    p.pb_msg.pin = 13;
    p.pb_msg.mode = 1;
    return p;
  }());
  test_roundtrip([]() {
    rpc::payload::DigitalWrite p;
    p.pb_msg.pin = 2;
    p.pb_msg.value = 1;
    return p;
  }());
  test_roundtrip([]() {
    rpc::payload::AnalogWrite p;
    p.pb_msg.pin = 5;
    p.pb_msg.value = 128;
    return p;
  }());
  test_roundtrip([]() {
    rpc::payload::PinRead p;
    p.pb_msg.pin = 7;
    return p;
  }());
  test_roundtrip([]() {
    rpc::payload::DigitalReadResponse p;
    p.pb_msg.value = 1;
    return p;
  }());
  test_roundtrip([]() {
    rpc::payload::AnalogReadResponse p;
    p.pb_msg.value = 512;
    return p;
  }());

  const char* str = "test";
  rpc::payload::ConsoleWrite cw;
  rpc::payload::copy_to_pb_bytes(
      cw.pb_msg.data, reinterpret_cast<const uint8_t*>(str), 4);
  test_roundtrip(cw);

  rpc::payload::DatastorePut dp;
  strncpy(dp.pb_msg.key, str, 32);
  rpc::payload::copy_to_pb_bytes(
      dp.pb_msg.value, reinterpret_cast<const uint8_t*>(str), 4);
  test_roundtrip(dp);

  rpc::payload::DatastoreGet dg;
  strncpy(dg.pb_msg.key, str, 32);
  test_roundtrip(dg);

  rpc::payload::DatastoreGetResponse dgr;
  rpc::payload::copy_to_pb_bytes(
      dgr.pb_msg.value, reinterpret_cast<const uint8_t*>(str), 4);
  test_roundtrip(dgr);

  rpc::payload::MailboxPush mbp;
  rpc::payload::copy_to_pb_bytes(
      mbp.pb_msg.data, reinterpret_cast<const uint8_t*>(str), 4);
  test_roundtrip(mbp);

  test_roundtrip([]() {
    rpc::payload::MailboxProcessed p;
    p.pb_msg.message_id = 1;
    return p;
  }());
  test_roundtrip([]() {
    rpc::payload::MailboxAvailableResponse p;
    p.pb_msg.count = 5;
    return p;
  }());

  rpc::payload::MailboxReadResponse mbr;
  rpc::payload::copy_to_pb_bytes(
      mbr.pb_msg.content, reinterpret_cast<const uint8_t*>(str), 4);
  test_roundtrip(mbr);

  rpc::payload::FileWrite fw;
  strncpy(fw.pb_msg.path, str, 64);
  rpc::payload::copy_to_pb_bytes(
      fw.pb_msg.data, reinterpret_cast<const uint8_t*>(str), 4);
  test_roundtrip(fw);

  rpc::payload::FileRead fr;
  strncpy(fr.pb_msg.path, str, 64);
  test_roundtrip(fr);

  rpc::payload::FileRemove frm;
  strncpy(frm.pb_msg.path, str, 64);
  test_roundtrip(frm);

  rpc::payload::FileReadResponse frr;
  rpc::payload::copy_to_pb_bytes(
      frr.pb_msg.content, reinterpret_cast<const uint8_t*>(str), 4);
  test_roundtrip(frr);

  rpc::payload::ProcessRunAsync pra;
  strncpy(pra.pb_msg.command, str, 64);
  test_roundtrip(pra);

  test_roundtrip([]() {
    rpc::payload::ProcessRunAsyncResponse p;
    p.pb_msg.pid = 123;
    return p;
  }());
  test_roundtrip([]() {
    rpc::payload::ProcessPoll p;
    p.pb_msg.pid = 123;
    return p;
  }());

  rpc::payload::ProcessPollResponse ppr;
  ppr.pb_msg.status = 0;
  ppr.pb_msg.exit_code = 0;
  rpc::payload::copy_to_pb_bytes(
      ppr.pb_msg.stdout_data, reinterpret_cast<const uint8_t*>(str), 4);
  rpc::payload::copy_to_pb_bytes(
      ppr.pb_msg.stderr_data, reinterpret_cast<const uint8_t*>(str), 4);
  test_roundtrip(ppr);

  test_roundtrip([]() {
    rpc::payload::ProcessKill p;
    p.pb_msg.pid = 123;
    return p;
  }());
  test_roundtrip([]() {
    rpc::payload::AckPacket p;
    p.pb_msg.command_id = 42;
    return p;
  }());
  test_roundtrip([]() {
    rpc::payload::HandshakeConfig p;
    p.pb_msg.ack_timeout_ms = 200;
    p.pb_msg.ack_retry_limit = 5;
    p.pb_msg.response_timeout_ms = 2000;
    return p;
  }());
  test_roundtrip([]() {
    rpc::payload::SetBaudratePacket p;
    p.pb_msg.baudrate = 57600;
    return p;
  }());
  test_roundtrip(rpc::payload::LinkSync{});
  test_roundtrip(rpc::payload::EnterBootloader{});

  rpc::payload::SpiTransfer st;
  rpc::payload::copy_to_pb_bytes(
      st.pb_msg.data, reinterpret_cast<const uint8_t*>(str), 4);
  test_roundtrip(st);

  rpc::payload::SpiTransferResponse strr;
  rpc::payload::copy_to_pb_bytes(
      strr.pb_msg.data, reinterpret_cast<const uint8_t*>(str), 4);
  test_roundtrip(strr);

  test_roundtrip([]() {
    rpc::payload::SpiConfig p;
    p.pb_msg.frequency = 1;
    p.pb_msg.bit_order = 2;
    p.pb_msg.data_mode = 3;
    return p;
  }());
}

void test_all_structs_chaos() {
  test_chaos_decode<rpc::payload::VersionResponse>();
  test_chaos_decode<rpc::payload::Capabilities>();
  test_chaos_decode<rpc::payload::ConsoleWrite>();
  test_chaos_decode<rpc::payload::DatastorePut>();
  test_chaos_decode<rpc::payload::FileWrite>();
  test_chaos_decode<rpc::payload::ProcessPollResponse>();
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_all_structs_roundtrip);
  RUN_TEST(test_all_structs_chaos);
  return UNITY_END();
}