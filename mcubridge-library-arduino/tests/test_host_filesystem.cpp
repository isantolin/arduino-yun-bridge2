#define BRIDGE_ENABLE_TEST_INTERFACE 1

#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif
#include <errno.h>
#include <etl/array.h>
#include <etl/string.h>
#include <pw_transfer/internal/chunk.h>
#include <stdio.h>
#include <sys/stat.h>
#include <unity.h>

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "hal/hal.h"
#include "services/FileSystem.h"
#include "test_support.h"

// Global stubs for host environment
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;
void setUp(void) {}
void tearDown(void) {}

namespace {
using bridge::test::TestAccessor;

bool create_host_test_file(const char* name, pw::span<const uint8_t> data) {
  if (::mkdir("/tmp/mcubridge-host-fs", 0755) != 0 && errno != EEXIST) {
    return false;
  }
  etl::string<128> path("/tmp/mcubridge-host-fs/");
  path.append(name);
  FILE* f = fopen(path.c_str(), "wb");
  if (f == nullptr) return false;
  const size_t written = fwrite(data.data(), 1U, data.size(), f);
  fclose(f);
  return written == data.size();
}

void test_hal_remove_file_roundtrip() {
  const etl::array<uint8_t, 8> payload = {'m', 'c', 'u', '-',
                                          'd', 'a', 't', 'a'};
  TEST_ASSERT_TRUE(create_host_test_file(
      "test_hal.bin", pw::span<const uint8_t>(payload.data(), payload.size())));
  TEST_ASSERT_TRUE(bridge::hal::removeFile("test_hal.bin").ok());
  TEST_ASSERT_FALSE(bridge::hal::removeFile("test_hal.bin").ok());
  TEST_ASSERT_TRUE(
      bridge::hal::removeFile("../escape.bin").IsInvalidArgument());
}

void test_filesystem_api_remove() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  FileSystem.remove("api_rem.bin");
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
  TEST_ASSERT_TRUE(stream.tx_buf.len > 0);
}

void test_filesystem_on_remove() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  const etl::array<uint8_t, 2> dummy = {1, 2};
  TEST_ASSERT_TRUE(create_host_test_file(
      "on_rem.bin", pw::span<const uint8_t>(dummy.data(), dummy.size())));
  rpc_pb_FileRemove msg = rpc_pb_FileRemove_init_default;
  bridge::pb::copy_string(msg.path, sizeof(msg.path), "on_rem.bin");
  FileSystem._onRemove(msg);
  TEST_ASSERT_FALSE(bridge::hal::removeFile("on_rem.bin").ok());
}

void test_filesystem_api_rejects_when_rpc_tx_is_unavailable() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setTxEnabled(false);
  TEST_ASSERT_FALSE(Bridge.isRpcTxReady());

  FileSystem.remove("offline.bin");
  TEST_ASSERT_FALSE(Bridge.isRpcTxReady());
}

void test_filesystem_api_empty_and_error_paths() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Empty remove
  FileSystem.remove("");
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

pw::Status g_fs_last_status = pw::OkStatus();
size_t g_fs_status_count = 0;

void capture_fs_status(pw::Status code, pw::span<const uint8_t>) {
  g_fs_last_status = code;
  ++g_fs_status_count;
}

bool find_tx_packet(const BiStream& stream,
                    pw::rpc::internal::pwpb::PacketType type,
                    uint32_t service_id, uint32_t method_id,
                    pw::rpc::internal::Packet& out_packet,
                    etl::array<uint8_t, 512>& payload_backing) {
  pw::hdlc::DecoderBuffer<1024> decoder;
  for (size_t i = 0; i < stream.tx_buf.len; ++i) {
    auto frame = decoder.Process(
        static_cast<pw::ByteSpan::element_type>(stream.tx_buf.data[i]));
    if (!frame.ok()) continue;
    auto pkt = pw::rpc::internal::Packet::FromBuffer(frame.value().data());
    if (!pkt.ok()) continue;
    if (pkt.value().type() != type || pkt.value().service_id() != service_id ||
        pkt.value().method_id() != method_id) {
      continue;
    }
    const auto p = pkt.value().payload();
    const size_t copy_len = etl::min(p.size(), payload_backing.size());
    for (size_t j = 0; j < copy_len; ++j) {
      payload_backing[j] = static_cast<uint8_t>(p[j]);
    }
    out_packet = pw::rpc::internal::Packet(
        pkt.value().type(), pkt.value().channel_id(), pkt.value().service_id(),
        pkt.value().method_id(), pkt.value().call_id(),
        pw::as_bytes(pw::span<const uint8_t>(payload_backing.data(), copy_len)),
        pkt.value().status());
    return true;
  }
  return false;
}

void send_transfer_read_open(TestAccessor& ba, uint32_t call_id = 1U) {
  TEST_ASSERT_TRUE(
      send_hdlc_rpc(ba, pw::tokenizer::Hash("pw.transfer.Transfer"),
                    pw::tokenizer::Hash("Read"), {},
                    pw::rpc::internal::pwpb::PacketType::REQUEST, call_id) > 0);
}

void send_transfer_read_chunk(TestAccessor& ba, uint32_t resource_id,
                              pw::transfer::internal::Chunk::Type type,
                              uint32_t call_id = 1U) {
  pw::transfer::internal::Chunk chunk(pw::transfer::ProtocolVersion::kLegacy,
                                      type);
  chunk.set_session_id(resource_id)
      .set_window_end_offset(64U)
      .set_max_chunk_size_bytes(32U);
  if (type != pw::transfer::internal::Chunk::Type::kStart) {
    chunk.set_offset(4U);
  }
  etl::array<pw::ByteSpan::element_type, 64> encoded_buf{};
  auto encoded =
      chunk.Encode(pw::ByteSpan(encoded_buf.data(), encoded_buf.size()));
  TEST_ASSERT_TRUE(encoded.ok());
  TEST_ASSERT_TRUE(
      send_hdlc_rpc(
          ba, pw::tokenizer::Hash("pw.transfer.Transfer"),
          pw::tokenizer::Hash("Read"),
          pw::span<const uint8_t>(
              reinterpret_cast<const uint8_t*>(encoded.value().data()),
              encoded.value().size()),
          pw::rpc::internal::pwpb::PacketType::CLIENT_STREAM, call_id) > 0);
}

void send_transfer_read_start_chunk(TestAccessor& ba, uint32_t resource_id,
                                    uint32_t call_id = 1U) {
  send_transfer_read_chunk(
      ba, resource_id, pw::transfer::internal::Chunk::Type::kStart, call_id);
}

void test_filesystem_upload_validation_and_unavailable() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  const etl::array<uint8_t, 4> payload = {'t', 'e', 's', 't'};
  TEST_ASSERT_FALSE(FileSystem.uploadPending());

  // Empty path and empty data are rejected without starting an upload.
  TEST_ASSERT_FALSE(FileSystem.upload(
      "", pw::span<const uint8_t>(payload.data(), payload.size())));
  TEST_ASSERT_FALSE(FileSystem.upload("valid.bin", pw::span<const uint8_t>()));

  // Path equal to or longer than the protobuf char buffer (leaving no NUL).
  etl::array<char, sizeof(rpc_pb_FileUpload{}.path)> long_path{};
  long_path.fill('a');
  TEST_ASSERT_FALSE(FileSystem.upload(
      etl::string_view(long_path.data(), long_path.size()),
      pw::span<const uint8_t>(payload.data(), payload.size())));

  // Oversized buffer (> UINT32_MAX) on 64-bit hosts is rejected before access.
  if constexpr (sizeof(size_t) > sizeof(uint32_t)) {
    const pw::span<const uint8_t> huge_span(
        payload.data(), static_cast<size_t>(UINT32_MAX) + 1U);
    TEST_ASSERT_FALSE(FileSystem.upload("huge.bin", huge_span));
  }

  // Unavailable RPC TX emits UNAVAILABLE and returns false.
  g_fs_last_status = pw::OkStatus();
  g_fs_status_count = 0;
  Bridge.onStatus(BridgeClass::StatusHandler::create<capture_fs_status>());
  ba.setTxEnabled(false);
  TEST_ASSERT_FALSE(FileSystem.upload(
      "offline.bin", pw::span<const uint8_t>(payload.data(), payload.size())));
  TEST_ASSERT_EQUAL_UINT(1U, g_fs_status_count);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(pw::Status::Unavailable().code()),
                        static_cast<int>(g_fs_last_status.code()));
  TEST_ASSERT_FALSE(FileSystem.uploadPending());
  Bridge.onStatus(BridgeClass::StatusHandler());
}

void test_filesystem_upload_transfer_read_armed_and_disarmed() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // 1. Disarmed PrepareRead: daemon requests FILE_UPLOAD_RESOURCE_ID before
  // any sketch upload is armed -> handler returns FAILED_PRECONDITION.
  send_transfer_read_open(ba, 10U);
  stream.tx_buf.clear();
  send_transfer_read_start_chunk(ba, rpc::RPC_FILE_UPLOAD_RESOURCE_ID, 10U);
  Bridge.process();

  pw::rpc::internal::Packet chunk_pkt;
  etl::array<uint8_t, 512> chunk_payload{};
  TEST_ASSERT_TRUE(
      find_tx_packet(stream, pw::rpc::internal::pwpb::PacketType::SERVER_STREAM,
                     pw::tokenizer::Hash("pw.transfer.Transfer"),
                     pw::tokenizer::Hash("Read"), chunk_pkt, chunk_payload));
  auto parsed_err = pw::transfer::internal::Chunk::Parse(chunk_pkt.payload());
  TEST_ASSERT_TRUE(parsed_err.ok());
  TEST_ASSERT_TRUE(parsed_err.value().status().has_value());
  // Upstream pw_transfer maps non-permission/non-unimplemented PrepareRead()
  // failures (FailedPrecondition from disarmed UploadTransferHandler) to
  // DATA_LOSS on the wire.
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(pw::Status::DataLoss().code()),
      static_cast<int>(parsed_err.value().status().value().code()));

  // 2. Armed upload + concurrent rejection + real pw_transfer read + OK finish.
  const etl::array<uint8_t, 10> upload_data = {'m', 'c', 'u', '-', 'u',
                                               'p', 'l', 'o', 'a', 'd'};
  stream.tx_buf.clear();
  TEST_ASSERT_TRUE(FileSystem.upload(
      "logs/telemetry.bin",
      pw::span<const uint8_t>(upload_data.data(), upload_data.size())));
  TEST_ASSERT_TRUE(FileSystem.uploadPending());

  // Concurrent upload must be rejected while one is in progress.
  TEST_ASSERT_FALSE(FileSystem.upload(
      "logs/second.bin",
      pw::span<const uint8_t>(upload_data.data(), upload_data.size())));

  // Verify the outgoing FileSystemService.Upload RPC request payload.
  pw::rpc::internal::Packet upload_req;
  etl::array<uint8_t, 512> upload_req_payload{};
  TEST_ASSERT_TRUE(find_tx_packet(
      stream, pw::rpc::internal::pwpb::PacketType::REQUEST,
      pw::tokenizer::Hash("rpc.pb.FileSystemService"),
      pw::tokenizer::Hash("Upload"), upload_req, upload_req_payload));
  rpc_pb_FileUpload decoded_req = rpc_pb_FileUpload_init_default;
  pb_istream_t istream = pb_istream_from_buffer(
      reinterpret_cast<const uint8_t*>(upload_req.payload().data()),
      upload_req.payload().size());
  TEST_ASSERT_TRUE(pb_decode(&istream, rpc_pb_FileUpload_fields, &decoded_req));
  TEST_ASSERT_EQUAL_STRING("logs/telemetry.bin", decoded_req.path);
  TEST_ASSERT_EQUAL_UINT32(upload_data.size(), decoded_req.size);

  // Trigger pw_transfer read against the armed UploadTransferHandler.
  stream.tx_buf.clear();
  send_transfer_read_start_chunk(ba, rpc::RPC_FILE_UPLOAD_RESOURCE_ID, 10U);
  Bridge.process();

  pw::rpc::internal::Packet data_pkt;
  etl::array<uint8_t, 512> data_pkt_payload{};
  TEST_ASSERT_TRUE(
      find_tx_packet(stream, pw::rpc::internal::pwpb::PacketType::SERVER_STREAM,
                     pw::tokenizer::Hash("pw.transfer.Transfer"),
                     pw::tokenizer::Hash("Read"), data_pkt, data_pkt_payload));
  auto parsed_data = pw::transfer::internal::Chunk::Parse(data_pkt.payload());
  TEST_ASSERT_TRUE(parsed_data.ok());
  TEST_ASSERT_EQUAL_UINT(upload_data.size(),
                         parsed_data.value().payload().size());
  TEST_ASSERT_EQUAL_UINT8_ARRAY(upload_data.data(),
                                parsed_data.value().payload().data(),
                                upload_data.size());

  // Complete the Upload RPC with OK -> disarms handler and clears pending.
  ba.invokeRpcResponse(
      pw::rpc::internal::Packet::Response(upload_req, pw::OkStatus()));
  TEST_ASSERT_FALSE(FileSystem.uploadPending());
  TEST_ASSERT_TRUE(FileSystem.lastUploadStatus().ok());
}

void test_filesystem_upload_error_reset_and_unimplemented_server_rpc() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  const etl::array<uint8_t, 4> payload = {0xAAU, 0xBBU, 0xCCU, 0xDDU};
  stream.tx_buf.clear();
  TEST_ASSERT_TRUE(FileSystem.upload(
      "err.bin", pw::span<const uint8_t>(payload.data(), payload.size())));
  TEST_ASSERT_TRUE(FileSystem.uploadPending());

  pw::rpc::internal::Packet upload_req;
  etl::array<uint8_t, 512> upload_req_payload{};
  TEST_ASSERT_TRUE(find_tx_packet(
      stream, pw::rpc::internal::pwpb::PacketType::REQUEST,
      pw::tokenizer::Hash("rpc.pb.FileSystemService"),
      pw::tokenizer::Hash("Upload"), upload_req, upload_req_payload));

  // Deliver a SERVER_ERROR -> invokes on_error -> updates lastUploadStatus.
  ba.invokeRpcResponse(pw::rpc::internal::Packet::ServerError(
      upload_req, pw::Status::PermissionDenied()));
  TEST_ASSERT_FALSE(FileSystem.uploadPending());
  TEST_ASSERT_EQUAL_INT(static_cast<int>(pw::Status::PermissionDenied().code()),
                        static_cast<int>(FileSystem.lastUploadStatus().code()));

  // Start another upload and clear it with _resetUpload().
  TEST_ASSERT_TRUE(FileSystem.upload(
      "reset.bin", pw::span<const uint8_t>(payload.data(), payload.size())));
  TEST_ASSERT_TRUE(FileSystem.uploadPending());
  FileSystem._resetUpload();
  TEST_ASSERT_FALSE(FileSystem.uploadPending());
  TEST_ASSERT_EQUAL_INT(static_cast<int>(pw::Status::Aborted().code()),
                        static_cast<int>(FileSystem.lastUploadStatus().code()));

  // MCU server rejects inbound FileSystemService.Upload with UNIMPLEMENTED.
  stream.tx_buf.clear();
  rpc_pb_FileUpload inbound = rpc_pb_FileUpload_init_default;
  bridge::pb::copy_string(inbound.path, sizeof(inbound.path), "nope.bin");
  inbound.size = 4U;
  TEST_ASSERT_TRUE(
      send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.FileSystemService"),
                       pw::tokenizer::Hash("Upload"), rpc_pb_FileUpload_fields,
                       inbound, 77U) > 0);
  pw::rpc::internal::Packet resp_pkt;
  etl::array<uint8_t, 512> resp_payload{};
  TEST_ASSERT_TRUE(
      find_tx_packet(stream, pw::rpc::internal::pwpb::PacketType::RESPONSE,
                     pw::tokenizer::Hash("rpc.pb.FileSystemService"),
                     pw::tokenizer::Hash("Upload"), resp_pkt, resp_payload));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(pw::Status::Unimplemented().code()),
                        static_cast<int>(resp_pkt.status().code()));
  // MCU server rejects retired FileSystemService.Write with NOT_FOUND.
  stream.tx_buf.clear();
  TEST_ASSERT_TRUE(
      send_hdlc_rpc(ba, pw::tokenizer::Hash("rpc.pb.FileSystemService"),
                    pw::tokenizer::Hash("Write"), {},
                    pw::rpc::internal::pwpb::PacketType::REQUEST, 78U) > 0);
  pw::rpc::internal::Packet err_pkt;
  etl::array<uint8_t, 512> err_payload{};
  TEST_ASSERT_TRUE(
      find_tx_packet(stream, pw::rpc::internal::pwpb::PacketType::SERVER_ERROR,
                     pw::tokenizer::Hash("rpc.pb.FileSystemService"),
                     pw::tokenizer::Hash("Write"), err_pkt, err_payload));
  TEST_ASSERT_EQUAL_INT(static_cast<int>(pw::Status::NotFound().code()),
                        static_cast<int>(err_pkt.status().code()));
}

void test_transfer_handler_and_bridge_lifecycle_regression() {
  BiStream stream;
  // Repeated reset_bridge_core cycles must cleanly detach and re-attach
  // g_upload_handler and static RPC services without intrusive-list crashes.
  for (int i = 0; i < 3; ++i) {
    stream.clear();
    reset_bridge_core(Bridge, stream);
  }

  // Stack-allocated BridgeClass constructed, registered (taking ownership from
  // global Bridge), processed, and destroyed cleanly at scope exit.
  {
    BiStream local_stream;
    BridgeClass local_bridge(local_stream);
    local_bridge.begin(0, "");
    local_bridge.process();
    // Re-calling begin() on the same instance must be idempotent.
    local_bridge.begin(0, "");
  }

  // Global Bridge can be re-initialized and handle cooperative pw_transfer
  // queue overflow cleanly in BridgeClass::process().
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  send_transfer_read_open(ba, 20U);

  g_fs_last_status = pw::OkStatus();
  g_fs_status_count = 0;
  Bridge.onStatus(BridgeClass::StatusHandler::create<capture_fs_status>());
  // Enqueue 3 non-initial chunks without polling -> overflows the 2-slot
  // cooperative event queue (ProcessServerChunk enqueues directly).
  send_transfer_read_chunk(
      ba, rpc::RPC_FILE_UPLOAD_RESOURCE_ID,
      pw::transfer::internal::Chunk::Type::kParametersContinue, 20U);
  send_transfer_read_chunk(
      ba, rpc::RPC_FILE_UPLOAD_RESOURCE_ID,
      pw::transfer::internal::Chunk::Type::kParametersContinue, 20U);
  send_transfer_read_chunk(
      ba, rpc::RPC_FILE_UPLOAD_RESOURCE_ID,
      pw::transfer::internal::Chunk::Type::kParametersContinue, 20U);
  Bridge.process();
  TEST_ASSERT_TRUE(g_fs_status_count >= 1U);
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(pw::Status::ResourceExhausted().code()),
      static_cast<int>(g_fs_last_status.code()));
  Bridge.onStatus(BridgeClass::StatusHandler());

  // Destroying Bridge while events are queued must still detach cleanly.
  send_transfer_read_chunk(
      ba, rpc::RPC_FILE_UPLOAD_RESOURCE_ID,
      pw::transfer::internal::Chunk::Type::kParametersContinue, 20U);
  send_transfer_read_chunk(
      ba, rpc::RPC_FILE_UPLOAD_RESOURCE_ID,
      pw::transfer::internal::Chunk::Type::kParametersContinue, 20U);
  reset_bridge_core(Bridge, stream);
}

void test_system_service_xoff_xon_rpc_handlers() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc_pb_SubscribeRequest req = rpc_pb_SubscribeRequest_init_default;
  stream.tx_buf.clear();
  TEST_ASSERT_TRUE(
      send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                       pw::tokenizer::Hash("Xoff"),
                       rpc_pb_SubscribeRequest_fields, req, 31U) > 0);
  pw::rpc::internal::Packet xoff_resp;
  etl::array<uint8_t, 512> resp_payload{};
  TEST_ASSERT_TRUE(
      find_tx_packet(stream, pw::rpc::internal::pwpb::PacketType::RESPONSE,
                     pw::tokenizer::Hash("rpc.pb.SystemService"),
                     pw::tokenizer::Hash("Xoff"), xoff_resp, resp_payload));
  TEST_ASSERT_TRUE(xoff_resp.status().ok());

  stream.tx_buf.clear();
  TEST_ASSERT_TRUE(
      send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                       pw::tokenizer::Hash("Xon"),
                       rpc_pb_SubscribeRequest_fields, req, 32U) > 0);
  pw::rpc::internal::Packet xon_resp;
  TEST_ASSERT_TRUE(
      find_tx_packet(stream, pw::rpc::internal::pwpb::PacketType::RESPONSE,
                     pw::tokenizer::Hash("rpc.pb.SystemService"),
                     pw::tokenizer::Hash("Xon"), xon_resp, resp_payload));
  TEST_ASSERT_TRUE(xon_resp.status().ok());
}

}  // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_hal_remove_file_roundtrip);
  RUN_TEST(test_filesystem_api_remove);
  RUN_TEST(test_filesystem_on_remove);
  RUN_TEST(test_filesystem_api_rejects_when_rpc_tx_is_unavailable);
  RUN_TEST(test_filesystem_api_empty_and_error_paths);
  RUN_TEST(test_filesystem_upload_validation_and_unavailable);
  RUN_TEST(test_filesystem_upload_transfer_read_armed_and_disarmed);
  RUN_TEST(test_filesystem_upload_error_reset_and_unimplemented_server_rpc);
  RUN_TEST(test_transfer_handler_and_bridge_lifecycle_regression);
  RUN_TEST(test_system_service_xoff_xon_rpc_handlers);
  return UNITY_END();
}
