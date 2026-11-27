#define BRIDGE_ENABLE_TEST_INTERFACE 1

#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif
#include <errno.h>
#include <string.h>
#include <etl/array.h>
#include <etl/string.h>
#include <pw_transfer/internal/chunk.h>
#include <pw_thread/thread.h>
#include <pw_thread_stl/options.h>
#include <stdio.h>
#include <sys/stat.h>
#include "pw_unit_test/framework.h"

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "hal/hal.h"
#include "services/FileSystem.h"
#include "test_support.h"

// Global stubs for host environment
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;
namespace {
using bridge::test::TestAccessor;

// Upstream pw_transfer::Thread is a ThreadCore. The host integration test must
// run it on Pigweed's STL thread backend, just as production RTOS targets run
// it on their system thread.
class HostTransferThread {
 public:
  explicit HostTransferThread(TestAccessor& bridge)
      : transfer_thread_(bridge.transferThread()),
        worker_(pw::thread::stl::Options(), transfer_thread_) {}

  HostTransferThread(const HostTransferThread&) = delete;
  HostTransferThread& operator=(const HostTransferThread&) = delete;

  ~HostTransferThread() {
    transfer_thread_.Terminate();
    worker_.join();
  }

  void waitUntilIdle() { transfer_thread_.WaitUntilEventIsProcessed(); }

 private:
  pw::transfer::Thread<1, 1>& transfer_thread_;
  pw::thread::Thread worker_;
};

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

TEST(HostFilesystem, HalRemoveFileRoundtrip) {
  const etl::array<uint8_t, 8> payload = {'m', 'c', 'u', '-',
                                          'd', 'a', 't', 'a'};
  EXPECT_TRUE(create_host_test_file(
      "test_hal.bin", pw::span<const uint8_t>(payload.data(), payload.size())));
  EXPECT_TRUE(bridge::hal::removeFile("test_hal.bin").ok());
  EXPECT_FALSE(bridge::hal::removeFile("test_hal.bin").ok());
  EXPECT_TRUE(bridge::hal::removeFile("../escape.bin").IsInvalidArgument());
}

TEST(HostFilesystem, FilesystemApiRemove) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  FileSystem.remove("api_rem.bin");
  EXPECT_TRUE(Bridge.isSynchronized());
  EXPECT_TRUE(stream.tx_buf.len > 0);
}

TEST(HostFilesystem, FilesystemOnRemove) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  const etl::array<uint8_t, 2> dummy = {1, 2};
  EXPECT_TRUE(create_host_test_file(
      "on_rem.bin", pw::span<const uint8_t>(dummy.data(), dummy.size())));
  rpc_pb_FileRemove msg = rpc_pb_FileRemove_init_default;
  bridge::pb::copy_string(msg.path, sizeof(msg.path), "on_rem.bin");
  FileSystem._onRemove(msg);
  EXPECT_FALSE(bridge::hal::removeFile("on_rem.bin").ok());
}

TEST(HostFilesystem, FilesystemApiRejectsWhenRpcTxIsUnavailable) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setTxEnabled(false);
  EXPECT_FALSE(Bridge.isRpcTxReady());

  FileSystem.remove("offline.bin");
  EXPECT_FALSE(Bridge.isRpcTxReady());
}

TEST(HostFilesystem, FilesystemApiEmptyAndErrorPaths) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Empty remove
  FileSystem.remove("");
  EXPECT_TRUE(Bridge.isSynchronized());
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
  EXPECT_TRUE(send_hdlc_rpc(ba, pw::tokenizer::Hash("pw.transfer.Transfer"),
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
  EXPECT_TRUE(encoded.ok());
  EXPECT_TRUE(send_hdlc_rpc(
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

TEST(HostFilesystem, FilesystemUploadValidationAndUnavailable) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  const etl::array<uint8_t, 4> payload = {'t', 'e', 's', 't'};
  EXPECT_FALSE(FileSystem.uploadPending());

  // Empty path and empty data are rejected without starting an upload.
  EXPECT_FALSE(FileSystem.upload(
      "", pw::span<const uint8_t>(payload.data(), payload.size())));
  EXPECT_FALSE(FileSystem.upload("valid.bin", pw::span<const uint8_t>()));

  // Path equal to or longer than the protobuf char buffer (leaving no NUL).
  etl::array<char, sizeof(rpc_pb_FileUpload{}.path)> long_path{};
  long_path.fill('a');
  EXPECT_FALSE(FileSystem.upload(
      etl::string_view(long_path.data(), long_path.size()),
      pw::span<const uint8_t>(payload.data(), payload.size())));

  // Oversized buffer (> UINT32_MAX) on 64-bit hosts is rejected before access.
  if constexpr (sizeof(size_t) > sizeof(uint32_t)) {
    const pw::span<const uint8_t> huge_span(
        payload.data(), static_cast<size_t>(UINT32_MAX) + 1U);
    EXPECT_FALSE(FileSystem.upload("huge.bin", huge_span));
  }

  // Unavailable RPC TX emits UNAVAILABLE and returns false.
  g_fs_last_status = pw::OkStatus();
  g_fs_status_count = 0;
  Bridge.onStatus(BridgeClass::StatusHandler::create<capture_fs_status>());
  ba.setTxEnabled(false);
  EXPECT_FALSE(FileSystem.upload(
      "offline.bin", pw::span<const uint8_t>(payload.data(), payload.size())));
  EXPECT_EQ(1U, g_fs_status_count);
  EXPECT_EQ(static_cast<int>(pw::Status::Unavailable().code()), static_cast<int>(g_fs_last_status.code()));
  EXPECT_FALSE(FileSystem.uploadPending());
  Bridge.onStatus(BridgeClass::StatusHandler());
}

TEST(HostFilesystem, FilesystemUploadTransferReadArmedAndDisarmed) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  HostTransferThread transfer_thread(ba);
  ba.setSynchronized();

  // 1. Disarmed PrepareRead: daemon requests FILE_UPLOAD_RESOURCE_ID before
  // any sketch upload is armed -> handler returns FAILED_PRECONDITION.
  send_transfer_read_open(ba, 10U);
  stream.tx_buf.clear();
  send_transfer_read_start_chunk(ba, rpc::RPC_FILE_UPLOAD_RESOURCE_ID, 10U);
  Bridge.process();
  transfer_thread.waitUntilIdle();

  pw::rpc::internal::Packet chunk_pkt;
  etl::array<uint8_t, 512> chunk_payload{};
  EXPECT_TRUE(find_tx_packet(stream, pw::rpc::internal::pwpb::PacketType::SERVER_STREAM,
                     pw::tokenizer::Hash("pw.transfer.Transfer"),
                     pw::tokenizer::Hash("Read"), chunk_pkt, chunk_payload));
  auto parsed_err = pw::transfer::internal::Chunk::Parse(chunk_pkt.payload());
  EXPECT_TRUE(parsed_err.ok());
  EXPECT_TRUE(parsed_err.value().status().has_value());
  // Upstream pw_transfer maps non-permission/non-unimplemented PrepareRead()
  // failures (FailedPrecondition from disarmed UploadTransferHandler) to
  // DATA_LOSS on the wire.
  EXPECT_EQ(static_cast<int>(pw::Status::DataLoss().code()), static_cast<int>(parsed_err.value().status().value().code()));

  // 2. Armed upload + concurrent rejection + real pw_transfer read + OK finish.
  const etl::array<uint8_t, 10> upload_data = {'m', 'c', 'u', '-', 'u',
                                               'p', 'l', 'o', 'a', 'd'};
  stream.tx_buf.clear();
  EXPECT_TRUE(FileSystem.upload(
      "logs/telemetry.bin",
      pw::span<const uint8_t>(upload_data.data(), upload_data.size())));
  EXPECT_TRUE(FileSystem.uploadPending());

  // Concurrent upload must be rejected while one is in progress.
  EXPECT_FALSE(FileSystem.upload(
      "logs/second.bin",
      pw::span<const uint8_t>(upload_data.data(), upload_data.size())));

  // Verify the outgoing FileSystemService.Upload RPC request payload.
  pw::rpc::internal::Packet upload_req;
  etl::array<uint8_t, 512> upload_req_payload{};
  EXPECT_TRUE(find_tx_packet(
      stream, pw::rpc::internal::pwpb::PacketType::REQUEST,
      pw::tokenizer::Hash("rpc.pb.FileSystemService"),
      pw::tokenizer::Hash("Upload"), upload_req, upload_req_payload));
  rpc_pb_FileUpload decoded_req = rpc_pb_FileUpload_init_default;
  pb_istream_t istream = pb_istream_from_buffer(
      reinterpret_cast<const uint8_t*>(upload_req.payload().data()),
      upload_req.payload().size());
  EXPECT_TRUE(pb_decode(&istream, rpc_pb_FileUpload_fields, &decoded_req));
  EXPECT_STREQ("logs/telemetry.bin", decoded_req.path);
  EXPECT_EQ(upload_data.size(), decoded_req.size);

  // Trigger pw_transfer read against the armed UploadTransferHandler.
  stream.tx_buf.clear();
  send_transfer_read_start_chunk(ba, rpc::RPC_FILE_UPLOAD_RESOURCE_ID, 10U);
  Bridge.process();
  transfer_thread.waitUntilIdle();

  pw::rpc::internal::Packet data_pkt;
  etl::array<uint8_t, 512> data_pkt_payload{};
  EXPECT_TRUE(find_tx_packet(stream, pw::rpc::internal::pwpb::PacketType::SERVER_STREAM,
                     pw::tokenizer::Hash("pw.transfer.Transfer"),
                     pw::tokenizer::Hash("Read"), data_pkt, data_pkt_payload));
  auto parsed_data = pw::transfer::internal::Chunk::Parse(data_pkt.payload());
  EXPECT_TRUE(parsed_data.ok());
  EXPECT_EQ(upload_data.size(), parsed_data.value().payload().size());
  EXPECT_EQ(0, memcmp(upload_data.data(), parsed_data.value().payload().data(), upload_data.size()));

  // Complete the Upload RPC with OK -> disarms handler and clears pending.
  ba.invokeRpcResponse(
      pw::rpc::internal::Packet::Response(upload_req, pw::OkStatus()));
  EXPECT_FALSE(FileSystem.uploadPending());
  EXPECT_TRUE(FileSystem.lastUploadStatus().ok());

  // A late retransmit chunk on the still-open pw_transfer session after
  // disarm() must never invoke pure-virtual Stream::DoSeek/DoRead on a
  // destroyed MemoryReader; the empty live reader rejects the seek cleanly.
  stream.tx_buf.clear();
  send_transfer_read_chunk(
      ba, rpc::RPC_FILE_UPLOAD_RESOURCE_ID,
      pw::transfer::internal::Chunk::Type::kParametersRetransmit, 10U);
  Bridge.process();
  transfer_thread.waitUntilIdle();
  pw::rpc::internal::Packet late_pkt;
  etl::array<uint8_t, 512> late_payload{};
  EXPECT_TRUE(find_tx_packet(stream, pw::rpc::internal::pwpb::PacketType::SERVER_STREAM,
                     pw::tokenizer::Hash("pw.transfer.Transfer"),
                     pw::tokenizer::Hash("Read"), late_pkt, late_payload));
  auto parsed_late = pw::transfer::internal::Chunk::Parse(late_pkt.payload());
  EXPECT_TRUE(parsed_late.ok());
  EXPECT_TRUE(parsed_late.value().status().has_value());
  EXPECT_FALSE(parsed_late.value().status().value().ok());
}

TEST(HostFilesystem, FilesystemUploadErrorResetAndUnimplementedServerRpc) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  const etl::array<uint8_t, 4> payload = {0xAAU, 0xBBU, 0xCCU, 0xDDU};
  stream.tx_buf.clear();
  EXPECT_TRUE(FileSystem.upload(
      "err.bin", pw::span<const uint8_t>(payload.data(), payload.size())));
  EXPECT_TRUE(FileSystem.uploadPending());

  pw::rpc::internal::Packet upload_req;
  etl::array<uint8_t, 512> upload_req_payload{};
  EXPECT_TRUE(find_tx_packet(
      stream, pw::rpc::internal::pwpb::PacketType::REQUEST,
      pw::tokenizer::Hash("rpc.pb.FileSystemService"),
      pw::tokenizer::Hash("Upload"), upload_req, upload_req_payload));

  // Deliver a SERVER_ERROR -> invokes on_error -> updates lastUploadStatus.
  ba.invokeRpcResponse(pw::rpc::internal::Packet::ServerError(
      upload_req, pw::Status::PermissionDenied()));
  EXPECT_FALSE(FileSystem.uploadPending());
  EXPECT_EQ(static_cast<int>(pw::Status::PermissionDenied().code()), static_cast<int>(FileSystem.lastUploadStatus().code()));

  // Start another upload and clear it with _resetUpload().
  EXPECT_TRUE(FileSystem.upload(
      "reset.bin", pw::span<const uint8_t>(payload.data(), payload.size())));
  EXPECT_TRUE(FileSystem.uploadPending());
  FileSystem._resetUpload();
  EXPECT_FALSE(FileSystem.uploadPending());
  EXPECT_EQ(static_cast<int>(pw::Status::Aborted().code()), static_cast<int>(FileSystem.lastUploadStatus().code()));

  // MCU server rejects inbound FileSystemService.Upload with UNIMPLEMENTED.
  stream.tx_buf.clear();
  rpc_pb_FileUpload inbound = rpc_pb_FileUpload_init_default;
  bridge::pb::copy_string(inbound.path, sizeof(inbound.path), "nope.bin");
  inbound.size = 4U;
  EXPECT_TRUE(send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.FileSystemService"),
                       pw::tokenizer::Hash("Upload"), rpc_pb_FileUpload_fields,
                       inbound, 77U) > 0);
  pw::rpc::internal::Packet resp_pkt;
  etl::array<uint8_t, 512> resp_payload{};
  EXPECT_TRUE(find_tx_packet(stream, pw::rpc::internal::pwpb::PacketType::RESPONSE,
                     pw::tokenizer::Hash("rpc.pb.FileSystemService"),
                     pw::tokenizer::Hash("Upload"), resp_pkt, resp_payload));
  EXPECT_EQ(static_cast<int>(pw::Status::Unimplemented().code()), static_cast<int>(resp_pkt.status().code()));
  // MCU server rejects retired FileSystemService.Write with NOT_FOUND.
  stream.tx_buf.clear();
  EXPECT_TRUE(send_hdlc_rpc(ba, pw::tokenizer::Hash("rpc.pb.FileSystemService"),
                    pw::tokenizer::Hash("Write"), {},
                    pw::rpc::internal::pwpb::PacketType::REQUEST, 78U) > 0);
  pw::rpc::internal::Packet err_pkt;
  etl::array<uint8_t, 512> err_payload{};
  EXPECT_TRUE(find_tx_packet(stream, pw::rpc::internal::pwpb::PacketType::SERVER_ERROR,
                     pw::tokenizer::Hash("rpc.pb.FileSystemService"),
                     pw::tokenizer::Hash("Write"), err_pkt, err_payload));
  EXPECT_EQ(static_cast<int>(pw::Status::NotFound().code()), static_cast<int>(err_pkt.status().code()));
}

TEST(HostFilesystem, TransferHandlerAndBridgeLifecycleRegression) {
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
  EXPECT_TRUE(g_fs_status_count >= 1U);
  EXPECT_EQ(static_cast<int>(pw::Status::ResourceExhausted().code()), static_cast<int>(g_fs_last_status.code()));
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

TEST(HostFilesystem, SystemServiceXoffXonRpcHandlers) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc_pb_SubscribeRequest req = rpc_pb_SubscribeRequest_init_default;
  stream.tx_buf.clear();
  EXPECT_TRUE(send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                       pw::tokenizer::Hash("Xoff"),
                       rpc_pb_SubscribeRequest_fields, req, 31U) > 0);
  pw::rpc::internal::Packet xoff_resp;
  etl::array<uint8_t, 512> resp_payload{};
  EXPECT_TRUE(find_tx_packet(stream, pw::rpc::internal::pwpb::PacketType::RESPONSE,
                     pw::tokenizer::Hash("rpc.pb.SystemService"),
                     pw::tokenizer::Hash("Xoff"), xoff_resp, resp_payload));
  EXPECT_TRUE(xoff_resp.status().ok());

  stream.tx_buf.clear();
  EXPECT_TRUE(send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                       pw::tokenizer::Hash("Xon"),
                       rpc_pb_SubscribeRequest_fields, req, 32U) > 0);
  pw::rpc::internal::Packet xon_resp;
  EXPECT_TRUE(find_tx_packet(stream, pw::rpc::internal::pwpb::PacketType::RESPONSE,
                     pw::tokenizer::Hash("rpc.pb.SystemService"),
                     pw::tokenizer::Hash("Xon"), xon_resp, resp_payload));
  EXPECT_TRUE(xon_resp.status().ok());
}

}  // namespace

