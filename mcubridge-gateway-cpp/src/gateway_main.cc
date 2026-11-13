#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <poll.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>
#include <openssl/ssl.h>
#include <openssl/x509v3.h>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <unordered_map>
#include <vector>
#include "pw_bytes/span.h"
#include "pw_hdlc/decoder.h"
#include "pw_hdlc/encoded_size.h"
#include "pw_hdlc/encoder.h"
#include "pw_rpc/channel.h"
#include "pw_rpc/client.h"
#include "pw_rpc/server.h"
#include "pw_rpc/packet_meta.h"
#include "pw_status/status.h"
#include "pw_stream/stream.h"
#include "pw_thread/thread.h"
#include "pw_thread_stl/options.h"
#include "pw_transfer/client.h"
#include "pw_transfer/transfer.h"
#include "pw_transfer/transfer_thread.h"
#include "gateway/gateway_control.rpc.pwpb.h"
#include "mcu_wire.raw_rpc.pb.h"

namespace {
constexpr uint64_t kRpcHdlcAddress = 1;
constexpr uint32_t kTransferWindowBytes = 16U * 1024U;
constexpr size_t kTransferChunkBytes = 1024U;
constexpr size_t kRpcEncodeBytes = 2048U;
constexpr size_t kHdlcDecodeBytes = 4096U;
constexpr size_t kTxQueueBytes = 64U * 1024U;
constexpr size_t kMaxSessions = 256U;
constexpr std::string_view kDeviceIdentityPrefix = "spiffe://mcubridge/device/";
constexpr std::string_view kClientIdentityPrefix = "spiffe://mcubridge/client/";
std::atomic<bool> g_stopping{false};

void HandleSignal(int) { g_stopping.store(true); }

enum class PeerRole { kDevice, kClient };

struct PeerIdentity {
  PeerRole role;
  std::string id;
};

bool IsValidPrincipalId(std::string_view id) {
  if (id.empty() || id.size() > 128U) return false;
  for (size_t i = 0; i < id.size(); ++i) {
    const unsigned char ch = static_cast<unsigned char>(id[i]);
    const bool alpha_numeric = (ch >= 'a' && ch <= 'z') ||
                               (ch >= 'A' && ch <= 'Z') ||
                               (ch >= '0' && ch <= '9');
    if (i == 0U && !alpha_numeric) return false;
    if (!alpha_numeric && ch != '-' && ch != '_') return false;
  }
  return true;
}

std::optional<PeerIdentity> PeerIdentityFromCertificate(SSL* ssl) {
  if (SSL_get_verify_result(ssl) != X509_V_OK) return std::nullopt;
  X509* certificate = SSL_get1_peer_certificate(ssl);
  if (certificate == nullptr) return std::nullopt;
  auto* names = static_cast<GENERAL_NAMES*>(
      X509_get_ext_d2i(certificate, NID_subject_alt_name, nullptr, nullptr));
  std::optional<PeerIdentity> identity;
  size_t uri_san_count = 0;
  if (names != nullptr) {
    for (int i = 0; i < sk_GENERAL_NAME_num(names); ++i) {
      const GENERAL_NAME* name = sk_GENERAL_NAME_value(names, i);
      if (name == nullptr || name->type != GEN_URI) continue;
      if (++uri_san_count != 1U) { identity.reset(); break; }
      const ASN1_IA5STRING* uri = name->d.uniformResourceIdentifier;
      const unsigned char* raw = ASN1_STRING_get0_data(uri);
      const int raw_size = ASN1_STRING_length(uri);
      if (raw == nullptr || raw_size <= 0) continue;
      const std::string_view value(reinterpret_cast<const char*>(raw), static_cast<size_t>(raw_size));
      PeerRole role;
      std::string_view prefix;
      if (value.compare(0, kDeviceIdentityPrefix.size(), kDeviceIdentityPrefix) == 0) {
        role = PeerRole::kDevice;
        prefix = kDeviceIdentityPrefix;
      } else if (value.compare(0, kClientIdentityPrefix.size(), kClientIdentityPrefix) == 0) {
        role = PeerRole::kClient;
        prefix = kClientIdentityPrefix;
      } else {
        continue;
      }
      if (identity.has_value()) { identity.reset(); break; }
      const std::string_view principal_id = value.substr(prefix.size());
      if (!IsValidPrincipalId(principal_id)) { identity.reset(); break; }
      identity = PeerIdentity{role, std::string(principal_id)};
    }
  }
  GENERAL_NAMES_free(names);
  X509_free(certificate);
  if (uri_san_count != 1U) return std::nullopt;
  return identity;
}

class TlsSocket {
 public:
  TlsSocket(SSL* ssl, int fd) : ssl_(ssl), fd_(fd) {}
  TlsSocket(const TlsSocket&) = delete;
  TlsSocket& operator=(const TlsSocket&) = delete;
  bool SetNonBlocking() const {
    const int flags = fcntl(fd_, F_GETFL, 0);
    return flags >= 0 && fcntl(fd_, F_SETFL, flags | O_NONBLOCK) == 0;
  }
  bool Read(std::byte* destination, size_t capacity, size_t& bytes_read,
            const std::atomic<bool>& stopping) {
    bytes_read = 0;
    while (!stopping.load()) {
      int result;
      int ssl_error = SSL_ERROR_NONE;
      {
        std::lock_guard lock(ssl_mutex_);
        result = SSL_read_ex(ssl_, destination, capacity, &bytes_read);
        if (result != 1) ssl_error = SSL_get_error(ssl_, result);
      }
      if (result == 1) return bytes_read > 0;
      if (ssl_error == SSL_ERROR_WANT_READ) { if (!Wait(POLLIN, stopping)) return false; }
      else if (ssl_error == SSL_ERROR_WANT_WRITE) { if (!Wait(POLLOUT, stopping)) return false; }
      else return false;
    }
    return false;
  }
  bool Write(const std::byte* source, size_t size, const std::atomic<bool>& stopping) {
    size_t offset = 0;
    while (offset < size && !stopping.load()) {
      size_t written = 0;
      int result;
      int ssl_error = SSL_ERROR_NONE;
      {
        std::lock_guard lock(ssl_mutex_);
        result = SSL_write_ex(ssl_, source + offset, size - offset, &written);
        if (result != 1) ssl_error = SSL_get_error(ssl_, result);
      }
      if (result == 1) {
        if (written == 0) return false;
        offset += written;
      } else if (ssl_error == SSL_ERROR_WANT_READ) {
        if (!Wait(POLLIN, stopping)) return false;
      } else if (ssl_error == SSL_ERROR_WANT_WRITE) {
        if (!Wait(POLLOUT, stopping)) return false;
      } else return false;
    }
    return offset == size;
  }
  void Stop() const { shutdown(fd_, SHUT_RDWR); }
  ~TlsSocket() {
    if (ssl_ != nullptr) { SSL_shutdown(ssl_); SSL_free(ssl_); }
    if (fd_ >= 0) close(fd_);
  }
 private:
  bool Wait(short events, const std::atomic<bool>& stopping) const {
    pollfd descriptor{fd_, events, 0};
    while (!stopping.load()) {
      const int result = poll(&descriptor, 1, 500);
      if (result > 0) {
        if ((descriptor.revents & (POLLERR | POLLHUP | POLLNVAL)) != 0) return false;
        if ((descriptor.revents & events) != 0) return true;
      } else if (result < 0 && errno != EINTR) return false;
    }
    return false;
  }
  SSL* ssl_;
  int fd_;
  std::mutex ssl_mutex_;
};

class TlsQueueWriter final : public pw::stream::NonSeekableWriter {
 public:
  explicit TlsQueueWriter(size_t capacity) : capacity_(capacity) {}
  size_t AvailableWriteBytes() const {
    std::lock_guard lock(mutex_);
    return capacity_ - queued_bytes_;
  }
  bool Pop(std::vector<std::byte>& output, const std::atomic<bool>& stopping) {
    std::unique_lock lock(mutex_);
    ready_.wait_for(lock, std::chrono::milliseconds(250), [&] {
      return stopped_ || !queue_.empty() || stopping.load();
    });
    if (queue_.empty()) return false;
    output = std::move(queue_.front());
    queue_.pop_front();
    queued_bytes_ -= output.size();
    return true;
  }
  void Stop() {
    { std::lock_guard lock(mutex_); stopped_ = true; }
    ready_.notify_all();
  }
 protected:
  size_t ConservativeLimit(LimitType type) const override {
    return type == LimitType::kWrite ? AvailableWriteBytes() : 0U;
  }

 private:
  pw::Status DoWrite(pw::ConstByteSpan data) override {
    std::lock_guard lock(mutex_);
    if (stopped_) return pw::Status::FailedPrecondition();
    if (data.size_bytes() > capacity_ - queued_bytes_) return pw::Status::ResourceExhausted();
    queue_.emplace_back(data.begin(), data.end());
    queued_bytes_ += data.size_bytes();
    ready_.notify_one();
    return pw::OkStatus();
  }
  const size_t capacity_;
  mutable std::mutex mutex_;
  std::condition_variable ready_;
  std::deque<std::vector<std::byte>> queue_;
  size_t queued_bytes_{0};
  bool stopped_{false};
};

class HdlcChannelOutput final : public pw::rpc::ChannelOutput {
 public:
  HdlcChannelOutput(TlsQueueWriter& writer, uint64_t address)
      : ChannelOutput("mcubridge-tls-hdlc"), writer_(writer), address_(address) {}
  pw::Status Send(pw::span<const std::byte> packet) override {
    std::lock_guard lock(mutex_);
    const size_t frame_size = pw::hdlc::MaxEncodedFrameSize(address_, packet);
    if (frame_size > writer_.ConservativeWriteLimit()) return pw::Status::ResourceExhausted();
    return pw::hdlc::WriteUIFrame(address_, packet, writer_);
  }
 private:
  TlsQueueWriter& writer_;
  const uint64_t address_;
  std::mutex mutex_;
};

class GatewaySession;

class DeviceSessionRegistry {
 public:
  bool Register(const std::string& device_id, std::shared_ptr<GatewaySession> session) {
    std::lock_guard lock(mutex_);
    return sessions_.emplace(device_id, std::move(session)).second;
  }
  void Unregister(const std::string& device_id, const GatewaySession* session) {
    std::lock_guard lock(mutex_);
    const auto it = sessions_.find(device_id);
    if (it != sessions_.end() && it->second.get() == session) {
      sessions_.erase(it);
    }
  }
  std::shared_ptr<GatewaySession> Lookup(const std::string& device_id) {
    std::lock_guard lock(mutex_);
    const auto it = sessions_.find(device_id);
    return it != sessions_.end() ? it->second : nullptr;
  }
  size_t size() const {
    std::lock_guard lock(mutex_);
    return sessions_.size();
  }
 private:
  mutable std::mutex mutex_;
  std::unordered_map<std::string, std::shared_ptr<GatewaySession>> sessions_;
};

class ClientSessionRegistry {
 public:
  bool Register(const std::string& principal_id, const void* session) {
    std::lock_guard lock(mutex_);
    return sessions_.emplace(principal_id, session).second;
  }
  void Unregister(const std::string& principal_id, const void* session) {
    std::lock_guard lock(mutex_);
    const auto it = sessions_.find(principal_id);
    if (it != sessions_.end() && it->second == session) sessions_.erase(it);
  }
  size_t size() const {
    std::lock_guard lock(mutex_);
    return sessions_.size();
  }
 private:
  mutable std::mutex mutex_;
  std::unordered_map<std::string, const void*> sessions_;
};

class SessionControl {
 public:
  virtual ~SessionControl() = default;
  virtual void Run() = 0;
  virtual void Stop() = 0;
};

class DeviceTelemetryService final
    : public mcubridge::gateway::pw_rpc::pwpb::DeviceTelemetry::Service<
          DeviceTelemetryService> {
 public:
  explicit DeviceTelemetryService(const std::string& device_id)
      : device_id_(device_id) {}

  pw::Status ReportTelemetry(
      const mcubridge::gateway::pwpb::TelemetryReport::Message& request,
      mcubridge::gateway::pwpb::TelemetryResponse::Message& response) {
    std::cerr << "device=" << device_id_
              << " event=telemetry_reported metrics_bytes="
              << request.daemon_metrics_blob.size()
              << " status_bytes=" << request.system_status_blob.size()
              << '\n';
    response.acknowledged = true;
    return pw::OkStatus();
  }

 private:
  const std::string& device_id_;
};

class GatewaySession final : public SessionControl,
                             public std::enable_shared_from_this<GatewaySession> {
 public:
  GatewaySession(SSL* ssl, int fd, std::string device_id, DeviceSessionRegistry& registry)
      : socket_(ssl, fd), registry_(registry), channel_output_(tx_writer_, kRpcHdlcAddress),
        client_channels_{pw::rpc::Channel::Create<1>(&channel_output_)},
        server_channels_{pw::rpc::Channel::Create<1>(&channel_output_)},
        rpc_client_(client_channels_),
        rpc_server_(server_channels_),
        transfer_thread_(pw::ByteSpan(transfer_chunk_), pw::ByteSpan(transfer_encode_)),
        transfer_client_(rpc_client_, 1, transfer_thread_, kTransferWindowBytes),
        decoder_(pw::ByteSpan(decoder_storage_)), device_id_(std::move(device_id)),
        telemetry_service_(device_id_),
        edge_control_client_(rpc_client_, 1) {}
  GatewaySession(const GatewaySession&) = delete;
  GatewaySession& operator=(const GatewaySession&) = delete;

  void Run() override {
    if (!socket_.SetNonBlocking()) return;
    if (!registry_.Register(device_id_, shared_from_this())) {
      std::cerr << "device=" << device_id_ << " event=session_rejected reason=duplicate_session\n";
      return;
    }
    struct RegistrationGuard {
      DeviceSessionRegistry& registry;
      const std::string& device_id;
      const GatewaySession* session;
      ~RegistrationGuard() {
        registry.Unregister(device_id, session);
        std::cerr << "device=" << device_id << " event=session_unregistered\n";
      }
    } registration{registry_, device_id_, this};

    rpc_server_.RegisterService(telemetry_service_);

    // The edge MPU is the pw_transfer server. The central Gateway owns only
    // the upstream client endpoint and never registers a duplicate server.
    pw::thread::stl::Options transfer_options;
    transfer_worker_ = std::make_unique<pw::thread::Thread>(
        transfer_options, transfer_thread_.AsFunction());
    tx_worker_ = std::thread([this] { TransmitLoop(); });
    std::cerr << "device=" << device_id_ << " event=session_open\n";
    ReceiveLoop();
    stopping_.store(true);
    socket_.Stop();
    tx_writer_.Stop();
    if (tx_worker_.joinable()) tx_worker_.join();
    transfer_thread_.Terminate();
    if (transfer_worker_ != nullptr && transfer_worker_->joinable()) transfer_worker_->join();
    std::cerr << "device=" << device_id_ << " event=session_closed\n";
  }

  void Stop() override {
    stopping_.store(true);
    socket_.Stop();
    tx_writer_.Stop();
  }

  pw::Status Dispatch(
      const mcubridge::gateway::pwpb::DispatchRequest::Message& request,
      mcubridge::gateway::pwpb::DispatchResponse::Message& response) {
    if (stopping_.load()) {
      return pw::Status::Unavailable();
    }

    std::lock_guard session_lock(dispatch_mutex_);

    mcubridge::gateway::pwpb::EdgeDispatchRequest::Message edge_req{};
    edge_req.service_name = request.service_name;
    edge_req.method_name = request.method_name;
    edge_req.request_payload = request.request_payload;
    edge_req.timeout_ms = request.timeout_ms;

    struct DispatchContext {
      std::mutex cv_mutex;
      std::condition_variable cv;
      bool completed = false;
      pw::Status call_status = pw::OkStatus();
      uint32_t status_code = 0;
      std::vector<std::byte> response_payload;
    } ctx;

    auto call = edge_control_client_.Dispatch(
        edge_req,
        [&ctx](const mcubridge::gateway::pwpb::EdgeDispatchResponse::Message& edge_resp,
               pw::Status status) {
          std::lock_guard lock(ctx.cv_mutex);
          ctx.call_status = status;
          ctx.status_code = edge_resp.status_code;
          ctx.response_payload.assign(edge_resp.response_payload.begin(),
                                      edge_resp.response_payload.end());
          ctx.completed = true;
          ctx.cv.notify_one();
        },
        [&ctx](pw::Status status) {
          std::lock_guard lock(ctx.cv_mutex);
          ctx.call_status = status;
          ctx.completed = true;
          ctx.cv.notify_one();
        });

    if (!call.active()) {
      return pw::Status::Unavailable();
    }

    const uint32_t timeout = request.timeout_ms > 0 ? request.timeout_ms : 5000;
    std::unique_lock lock(ctx.cv_mutex);
    if (!ctx.cv.wait_for(lock, std::chrono::milliseconds(timeout), [&] {
          return ctx.completed || stopping_.load();
        })) {
      call.Cancel().IgnoreError();
      return pw::Status::DeadlineExceeded();
    }

    if (stopping_.load() && !ctx.completed) {
      call.Cancel().IgnoreError();
      return pw::Status::Unavailable();
    }

    if (!ctx.call_status.ok()) {
      return ctx.call_status;
    }

    response.status_code = ctx.status_code;
    response.response_payload.assign(ctx.response_payload.begin(), ctx.response_payload.end());
    return pw::OkStatus();
  }

 private:
  void TransmitLoop() {
    std::vector<std::byte> frame;
    while (!stopping_.load()) {
      if (!tx_writer_.Pop(frame, stopping_)) continue;
      if (!socket_.Write(frame.data(), frame.size(), stopping_)) {
        stopping_.store(true);
        socket_.Stop();
        break;
      }
    }
  }

  void ReceiveLoop() {
    std::array<std::byte, 4096> input{};
    while (!stopping_.load()) {
      size_t received = 0;
      if (!socket_.Read(input.data(), input.size(), received, stopping_)) break;
      for (size_t i = 0; i < received; ++i) {
        auto frame = decoder_.Process(input[i]);
        if (frame.status() == pw::Status::Unavailable()) continue;
        if (!frame.ok() || frame->address() != kRpcHdlcAddress) continue;
        auto meta = pw::rpc::PacketMeta::FromBuffer(frame->data());
        if (!meta.ok()) {
          std::cerr << "device=" << device_id_ << " event=rpc_packet_corrupt\n";
          continue;
        }
        pw::Status status;
        if (meta->destination_is_client()) {
          status = rpc_client_.ProcessPacket(frame->data());
        } else if (meta->destination_is_server()) {
          status = rpc_server_.ProcessPacket(frame->data());
        } else {
          status = pw::Status::InvalidArgument();
        }
        if (!status.ok() && status != pw::Status::Unavailable()) {
          std::cerr << "device=" << device_id_ << " event=rpc_packet_rejected status="
                    << status.code() << "\n";
        }
      }
    }
  }

  TlsSocket socket_;
  DeviceSessionRegistry& registry_;
  TlsQueueWriter tx_writer_{kTxQueueBytes};
  HdlcChannelOutput channel_output_;
  std::array<pw::rpc::Channel, 1> client_channels_;
  std::array<pw::rpc::Channel, 1> server_channels_;
  pw::rpc::Client rpc_client_;
  pw::rpc::Server rpc_server_;
  std::array<std::byte, kTransferChunkBytes> transfer_chunk_{};
  std::array<std::byte, kRpcEncodeBytes> transfer_encode_{};
  pw::transfer::Thread<1, 1> transfer_thread_;
  pw::transfer::Client transfer_client_;
  std::unique_ptr<pw::thread::Thread> transfer_worker_;
  std::array<std::byte, kHdlcDecodeBytes> decoder_storage_{};
  pw::hdlc::Decoder decoder_;
  std::string device_id_;
  DeviceTelemetryService telemetry_service_;
  mcubridge::gateway::pw_rpc::pwpb::EdgeControl::Client edge_control_client_;
  std::atomic<bool> stopping_{false};
  std::thread tx_worker_;
  std::mutex dispatch_mutex_;
};

class GatewayControlService final
    : public mcubridge::gateway::pw_rpc::pwpb::GatewayControl::Service<
          GatewayControlService> {
 public:
  GatewayControlService(DeviceSessionRegistry& devices, ClientSessionRegistry& clients)
      : devices_(devices), clients_(clients) {}

  pw::Status Health(const mcubridge::gateway::pwpb::HealthRequest::Message&,
                    mcubridge::gateway::pwpb::HealthResponse::Message& response) {
    response.ready = true;
    response.active_device_sessions = static_cast<uint32_t>(devices_.size());
    response.active_client_sessions = static_cast<uint32_t>(clients_.size());
    return pw::OkStatus();
  }

  pw::Status Dispatch(const mcubridge::gateway::pwpb::DispatchRequest::Message& request,
                      mcubridge::gateway::pwpb::DispatchResponse::Message& response) {
    const std::string target_id(request.target_device_id);
    auto session = devices_.Lookup(target_id);
    if (!session) {
      return pw::Status::NotFound();
    }
    return session->Dispatch(request, response);
  }

 private:
  DeviceSessionRegistry& devices_;
  ClientSessionRegistry& clients_;
};

class GatewayClientSession final : public SessionControl {
 public:
  GatewayClientSession(SSL* ssl,
                       int fd,
                       std::string client_id,
                       DeviceSessionRegistry& devices,
                       ClientSessionRegistry& clients)
      : socket_(ssl, fd), clients_(clients),
        control_service_(devices, clients),
        channel_output_(tx_writer_, kRpcHdlcAddress),
        channels_{pw::rpc::Channel::Create<1>(&channel_output_)},
        rpc_server_(channels_),
        decoder_(pw::ByteSpan(decoder_storage_)),
        client_id_(std::move(client_id)) {}
  GatewayClientSession(const GatewayClientSession&) = delete;
  GatewayClientSession& operator=(const GatewayClientSession&) = delete;

  void Run() override {
    if (!socket_.SetNonBlocking()) return;
    if (!clients_.Register(client_id_, this)) {
      std::cerr << "client=" << client_id_
                << " event=session_rejected reason=duplicate_session\n";
      return;
    }
    struct RegistrationGuard {
      ClientSessionRegistry& registry;
      const std::string& client_id;
      const void* session;
      ~RegistrationGuard() {
        registry.Unregister(client_id, session);
        std::cerr << "client=" << client_id << " event=session_unregistered\n";
      }
    } registration{clients_, client_id_, this};

    rpc_server_.RegisterService(control_service_);
    tx_worker_ = std::thread([this] { TransmitLoop(); });
    std::cerr << "client=" << client_id_ << " event=session_open\n";
    ReceiveLoop();
    stopping_.store(true);
    socket_.Stop();
    tx_writer_.Stop();
    if (tx_worker_.joinable()) tx_worker_.join();
    std::cerr << "client=" << client_id_ << " event=session_closed\n";
  }

  void Stop() override {
    stopping_.store(true);
    socket_.Stop();
    tx_writer_.Stop();
  }

 private:
  void TransmitLoop() {
    std::vector<std::byte> frame;
    while (!stopping_.load()) {
      if (!tx_writer_.Pop(frame, stopping_)) continue;
      if (!socket_.Write(frame.data(), frame.size(), stopping_)) {
        stopping_.store(true);
        socket_.Stop();
        break;
      }
    }
  }

  void ReceiveLoop() {
    std::array<std::byte, 4096> input{};
    while (!stopping_.load()) {
      size_t received = 0;
      if (!socket_.Read(input.data(), input.size(), received, stopping_)) break;
      for (size_t i = 0; i < received; ++i) {
        auto frame = decoder_.Process(input[i]);
        if (frame.status() == pw::Status::Unavailable()) continue;
        if (!frame.ok() || frame->address() != kRpcHdlcAddress) continue;
        const pw::Status status = rpc_server_.ProcessPacket(frame->data());
        if (!status.ok() && status != pw::Status::Unavailable()) {
          std::cerr << "client=" << client_id_ << " event=rpc_packet_rejected status="
                    << status.code() << "\n";
        }
      }
    }
  }

  TlsSocket socket_;
  ClientSessionRegistry& clients_;
  GatewayControlService control_service_;
  TlsQueueWriter tx_writer_{kTxQueueBytes};
  HdlcChannelOutput channel_output_;
  std::array<pw::rpc::Channel, 1> channels_;
  pw::rpc::Server rpc_server_;
  std::array<std::byte, kHdlcDecodeBytes> decoder_storage_{};
  pw::hdlc::Decoder decoder_;
  std::string client_id_;
  std::atomic<bool> stopping_{false};
  std::thread tx_worker_;
};

struct SessionTask {
  int fd{-1};
  std::atomic<bool> done{false};
  std::mutex mutex;
  std::shared_ptr<SessionControl> session;
  std::thread worker;
};
struct TlsContextDeleter { void operator()(SSL_CTX* context) const { SSL_CTX_free(context); } };
using TlsContext = std::unique_ptr<SSL_CTX, TlsContextDeleter>;

TlsContext CreateTlsContext(const std::string& certificate,
                            const std::string& private_key,
                            const std::string& client_ca,
                            std::string& error) {
  TlsContext context(SSL_CTX_new(TLS_server_method()));
  if (!context) {
    error = "SSL_CTX_new failed";
    return {};
  }
  if (SSL_CTX_set_min_proto_version(context.get(), TLS1_3_VERSION) != 1 ||
      SSL_CTX_use_certificate_chain_file(context.get(), certificate.c_str()) != 1 ||
      SSL_CTX_use_PrivateKey_file(context.get(), private_key.c_str(), SSL_FILETYPE_PEM) != 1 ||
      SSL_CTX_check_private_key(context.get()) != 1 ||
      SSL_CTX_load_verify_locations(context.get(), client_ca.c_str(), nullptr) != 1 ||
      SSL_CTX_set_purpose(context.get(), X509_PURPOSE_SSL_CLIENT) != 1) {
    error = "TLS certificate configuration failed";
    return {};
  }
  SSL_CTX_set_verify_depth(context.get(), 4);
  SSL_CTX_set_session_cache_mode(context.get(), SSL_SESS_CACHE_OFF);
  SSL_CTX_set_options(context.get(), SSL_OP_NO_RENEGOTIATION | SSL_OP_NO_TICKET);
  SSL_CTX_set_verify(context.get(), SSL_VERIFY_PEER | SSL_VERIFY_FAIL_IF_NO_PEER_CERT, nullptr);
  return context;
}
int CreateListener(const std::string& bind_address,
                   const std::string& port,
                   std::string& error) {
  addrinfo hints{};
  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = SOCK_STREAM;
  hints.ai_flags = AI_PASSIVE;
  addrinfo* addresses = nullptr;
  const char* host = bind_address == "*" ? nullptr : bind_address.c_str();
  const int lookup = getaddrinfo(host, port.c_str(), &hints, &addresses);
  if (lookup != 0) {
    error = gai_strerror(lookup);
    return -1;
  }
  int listener = -1;
  for (addrinfo* address = addresses; address != nullptr; address = address->ai_next) {
    listener = socket(address->ai_family, address->ai_socktype, address->ai_protocol);
    if (listener < 0) continue;
    int enabled = 1;
    setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &enabled, sizeof(enabled));
    if (bind(listener, address->ai_addr, address->ai_addrlen) == 0 &&
        listen(listener, 128) == 0) break;
    close(listener);
    listener = -1;
  }
  freeaddrinfo(addresses);
  if (listener < 0) error = "unable to bind TCP listener";
  return listener;
}
std::string Argument(int argc, char** argv, std::string_view name, std::string fallback = {}) {
  for (int i = 1; i + 1 < argc; ++i) {
    if (std::string_view(argv[i]) == name) return argv[i + 1];
  }
  return fallback;
}
void ReapTasks(std::vector<std::unique_ptr<SessionTask>>& tasks) {
  auto it = tasks.begin();
  while (it != tasks.end()) {
    if ((*it)->done.load()) {
      if ((*it)->worker.joinable()) (*it)->worker.join();
      it = tasks.erase(it);
    } else ++it;
  }
}
}  // namespace

int main(int argc, char** argv) {
  const std::string certificate = Argument(argc, argv, "--cert");
  const std::string private_key = Argument(argc, argv, "--key");
  const std::string client_ca = Argument(argc, argv, "--client-ca");
  const std::string bind_address = Argument(argc, argv, "--bind", "*");
  const std::string port = Argument(argc, argv, "--port", "8443");
  if (certificate.empty() || private_key.empty() || client_ca.empty()) {
    std::cerr << "usage: mcubridge-gateway-cpp --cert FILE --key FILE "
                 "--client-ca FILE [--bind ADDRESS] [--port PORT]\n";
    return 2;
  }
  std::string error;
  auto tls_context = CreateTlsContext(certificate, private_key, client_ca, error);
  if (!tls_context) {
    std::cerr << "fatal=" << error << '\n';
    return 1;
  }
  const int listener = CreateListener(bind_address, port, error);
  if (listener < 0) {
    std::cerr << "fatal=" << error << '\n';
    return 1;
  }
  signal(SIGINT, HandleSignal);
  signal(SIGTERM, HandleSignal);
  signal(SIGPIPE, SIG_IGN);
  DeviceSessionRegistry device_registry;
  ClientSessionRegistry client_registry;
  std::vector<std::unique_ptr<SessionTask>> tasks;
  std::cerr << "event=gateway_listening bind=" << bind_address
            << " port=" << port << " tls=1.3 identity=uri_san\n";
  while (!g_stopping.load()) {
    ReapTasks(tasks);
    pollfd descriptor{listener, POLLIN, 0};
    const int ready = poll(&descriptor, 1, 500);
    if (ready < 0) {
      if (errno == EINTR) continue;
      std::cerr << "fatal=listener poll failed\n";
      break;
    }
    if (ready == 0 || (descriptor.revents & POLLIN) == 0) continue;
    sockaddr_storage peer{};
    socklen_t peer_size = sizeof(peer);
    const int client_fd = accept(listener, reinterpret_cast<sockaddr*>(&peer), &peer_size);
    if (client_fd < 0) {
      if (errno == EINTR || errno == EAGAIN) continue;
      std::cerr << "event=accept_failed errno=" << errno << '\n';
      continue;
    }
    if (tasks.size() >= kMaxSessions) {
      close(client_fd);
      continue;
    }
    auto task = std::make_unique<SessionTask>();
    task->fd = client_fd;
    SessionTask* task_ptr = task.get();
    task->worker = std::thread(
        [task_ptr, context = tls_context.get(), &device_registry, &client_registry] {
      const auto close_task_fd = [task_ptr] {
        std::lock_guard lock(task_ptr->mutex);
        if (task_ptr->fd >= 0) {
          close(task_ptr->fd);
          task_ptr->fd = -1;
        }
      };
      int client_fd = -1;
      {
        std::lock_guard lock(task_ptr->mutex);
        client_fd = task_ptr->fd;
      }
      SSL* ssl = SSL_new(context);
      if (ssl == nullptr) {
        close_task_fd();
        task_ptr->done.store(true);
        return;
      }
      SSL_set_fd(ssl, client_fd);
      timeval handshake_timeout{5, 0};
      setsockopt(client_fd, SOL_SOCKET, SO_RCVTIMEO,
                 &handshake_timeout, sizeof(handshake_timeout));
      setsockopt(client_fd, SOL_SOCKET, SO_SNDTIMEO,
                 &handshake_timeout, sizeof(handshake_timeout));
      SSL_set_mode(ssl, SSL_MODE_ENABLE_PARTIAL_WRITE |
                            SSL_MODE_ACCEPT_MOVING_WRITE_BUFFER);
      if (SSL_accept(ssl) != 1) {
        std::cerr << "event=client_rejected reason=tls_handshake_failed\n";
        SSL_free(ssl);
        close_task_fd();
        task_ptr->done.store(true);
        return;
      }
      auto identity = PeerIdentityFromCertificate(ssl);
      if (!identity.has_value()) {
        std::cerr << "event=client_rejected reason=missing_or_invalid_uri_san\n";
        SSL_shutdown(ssl);
        SSL_free(ssl);
        close_task_fd();
        task_ptr->done.store(true);
        return;
      }
      std::shared_ptr<SessionControl> session;
      if (identity->role == PeerRole::kDevice) {
        session = std::make_shared<GatewaySession>(
            ssl, client_fd, std::move(identity->id), device_registry);
      } else {
        session = std::make_shared<GatewayClientSession>(
            ssl, client_fd, std::move(identity->id), device_registry, client_registry);
      }
      {
        std::lock_guard lock(task_ptr->mutex);
        task_ptr->session = session;
        task_ptr->fd = -1;
      }
      session->Run();
      task_ptr->done.store(true);
    });
    tasks.push_back(std::move(task));
  }
  close(listener);
  for (auto& task : tasks) {
    std::lock_guard lock(task->mutex);
    if (task->session != nullptr) task->session->Stop();
    else if (task->fd >= 0) shutdown(task->fd, SHUT_RDWR);
  }
  for (auto& task : tasks) if (task->worker.joinable()) task->worker.join();
  std::cerr << "event=gateway_stopped\n";
  return 0;
}
