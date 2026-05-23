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
    rpc_pb_VersionResponse p;
    p.major = 1;
    p.minor = 2;
    p.patch = 3;
    return p;
  }());
  test_roundtrip([]() {
    rpc_pb_FreeMemoryResponse p;
    p.value = 1024;
    return p;
  }());
  test_roundtrip([]() {
    rpc_pb_Capabilities p;
    p.ver = 1;
    p.arch = 2;
    p.dig = 20;
    p.ana = 6;
    p.feat = 0xFF;
    return p;
  }());
  test_roundtrip([]() {
    rpc_pb_PinMode p;
    p.pin = 13;
    p.mode = 1;
    return p;
  }());
  test_roundtrip([]() {
    rpc_pb_DigitalWrite p;
    p.pin = 2;
    p.value = 1;
    return p;
  }());
  test_roundtrip([]() {
    rpc_pb_AnalogWrite p;
    p.pin = 5;
    p.value = 128;
    return p;
  }());
  test_roundtrip([]() {
    rpc_pb_PinRead p;
    p.pin = 7;
    return p;
  }());
  test_roundtrip([]() {
    rpc_pb_DigitalReadResponse p;
    p.value = 1;
    return p;
  }());
  test_roundtrip([]() {
    rpc_pb_AnalogReadResponse p;
    p.value = 512;
    return p;
  }());

  const char* str = "test";
  rpc_pb_ConsoleWrite cw;
  rpc::payload::copy_to_pb_bytes(
      cw.data, reinterpret_cast<const uint8_t*>(str), 4);
  test_roundtrip(cw);

  rpc_pb_DatastorePut dp;
  strncpy(dp.key, str, 32);
  rpc::payload::copy_to_pb_bytes(
      dp.value, reinterpret_cast<const uint8_t*>(str), 4);
  test_roundtrip(dp);

  rpc_pb_DatastoreGet dg;
  strncpy(dg.key, str, 32);
  test_roundtrip(dg);

  rpc_pb_DatastoreGetResponse dgr;
  rpc::payload::copy_to_pb_bytes(
      dgr.value, reinterpret_cast<const uint8_t*>(str), 4);
  test_roundtrip(dgr);

  rpc_pb_MailboxPush mbp;
  rpc::payload::copy_to_pb_bytes(
      mbp.data, reinterpret_cast<const uint8_t*>(str), 4);
  test_roundtrip(mbp);

  test_roundtrip([]() {
    MailboxProcessed p;
    p.message_id = 1;
    return p;
  }());
  test_roundtrip([]() {
    rpc_pb_MailboxAvailableResponse p;
    p.count = 5;
    return p;
  }());

  rpc_pb_MailboxReadResponse mbr;
  rpc::payload::copy_to_pb_bytes(
      mbr.content, reinterpret_cast<const uint8_t*>(str), 4);
  test_roundtrip(mbr);

  rpc_pb_FileWrite fw;
  strncpy(fw.path, str, 64);
  rpc::payload::copy_to_pb_bytes(
      fw.data, reinterpret_cast<const uint8_t*>(str), 4);
  test_roundtrip(fw);

  rpc_pb_FileRead fr;
  strncpy(fr.path, str, 64);
  test_roundtrip(fr);

  rpc_pb_FileRemove frm;
  strncpy(frm.path, str, 64);
  test_roundtrip(frm);

  rpc_pb_FileReadResponse frr;
  rpc::payload::copy_to_pb_bytes(
      frr.content, reinterpret_cast<const uint8_t*>(str), 4);
  test_roundtrip(frr);

  ProcessRunAsync pra;
  strncpy(pra.command, str, 64);
  test_roundtrip(pra);

  test_roundtrip([]() {
    rpc_pb_ProcessRunAsyncResponse p;
    p.pid = 123;
    return p;
  }());
  test_roundtrip([]() {
    rpc_pb_ProcessPoll p;
    p.pid = 123;
    return p;
  }());

  rpc_pb_ProcessPollResponse ppr;
  ppr.status = 0;
  ppr.exit_code = 0;
  rpc::payload::copy_to_pb_bytes(
      ppr.stdout_data, reinterpret_cast<const uint8_t*>(str), 4);
  rpc::payload::copy_to_pb_bytes(
      ppr.stderr_data, reinterpret_cast<const uint8_t*>(str), 4);
  test_roundtrip(ppr);

  test_roundtrip([]() {
    rpc_pb_ProcessKill p;
    p.pid = 123;
    return p;
  }());
  test_roundtrip([]() {
    AckPacket p;
    p.command_id = 42;
    return p;
  }());
  test_roundtrip([]() {
    rpc_pb_HandshakeConfig p;
    p.ack_timeout_ms = 200;
    p.ack_retry_limit = 5;
    p.response_timeout_ms = 2000;
    return p;
  }());
  test_roundtrip([]() {
    rpc_pb_SetBaudratePacket p;
    p.baudrate = 57600;
    return p;
  }());
  test_roundtrip(LinkSync{});
  test_roundtrip(rpc_pb_EnterBootloader{});

  rpc_pb_SpiTransfer st;
  rpc::payload::copy_to_pb_bytes(
      st.data, reinterpret_cast<const uint8_t*>(str), 4);
  test_roundtrip(st);

  rpc_pb_SpiTransferResponse strr;
  rpc::payload::copy_to_pb_bytes(
      strr.data, reinterpret_cast<const uint8_t*>(str), 4);
  test_roundtrip(strr);

  test_roundtrip([]() {
    rpc_pb_SpiConfig p;
    p.frequency = 1;
    p.bit_order = 2;
    p.data_mode = 3;
    return p;
  }());
}

void test_all_structs_chaos() {
  test_chaos_decode<rpc_pb_VersionResponse>();
  test_chaos_decode<rpc_pb_Capabilities>();
  test_chaos_decode<rpc_pb_ConsoleWrite>();
  test_chaos_decode<rpc_pb_DatastorePut>();
  test_chaos_decode<rpc_pb_FileWrite>();
  test_chaos_decode<rpc_pb_ProcessPollResponse>();
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_all_structs_roundtrip);
  RUN_TEST(test_all_structs_chaos);
  return UNITY_END();
}