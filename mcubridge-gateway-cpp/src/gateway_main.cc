#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <openssl/ssl.h>
#include <openssl/x509v3.h>
#include <poll.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <unistd.h>

#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#include <sstream>
#include <string>
#include <string_view>
#include <unordered_map>
#include <utility>
#include <vector>

#define PW_LOG_MODULE_NAME "GW"

#include "gateway/gateway_control.rpc.pwpb.h"
#include "mcu_wire.raw_rpc.pb.h"
#include "pw_assert/check.h"
#include "pw_bytes/span.h"
#include "pw_chrono/system_clock.h"
#include "pw_hdlc/decoder.h"
#include "pw_hdlc/encoded_size.h"
#include "pw_hdlc/rpc_channel.h"
#include "pw_log/log.h"
#include "pw_log_basic/log_basic.h"
#include "pw_metric/metric.h"
#include "pw_metric/metric_service_pwpb.h"
#include "pw_result/result.h"
#include "pw_rpc/channel.h"
#include "pw_rpc/client.h"
#include "pw_rpc/packet_meta.h"
#include "pw_rpc/server.h"
#include "pw_status/status.h"
#include "pw_status/try.h"
#include "pw_stream/memory_stream.h"
#include "pw_stream/stream.h"
#include "pw_sync/timed_thread_notification.h"
#include "pw_thread/thread.h"
#include "pw_thread_stl/options.h"
#include "pw_transfer/client.h"
#include "pw_transfer/handler.h"
#include "pw_transfer/transfer.h"
#include "pw_transfer/transfer_thread.h"

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

// ADR-0007 staging layout. Every client and device gets a private directory
// under a root that the gateway owns (mode 0700, not a symlink); clients and
// devices live in separate namespaces so identities cannot collide.
constexpr uint32_t kDefaultTransferResource = 1U;
constexpr uint32_t kFileTransferResource = 2U;
constexpr uint32_t kEepromTransferResource = 0x4D454741U;
constexpr size_t kFileStagingMaxBytes = 1024U * 1024U;
// Must equal the MCU EEPROM resource size (EEPROM minus the 16-byte ADR-0005
// boot-epoch reservation).
constexpr size_t kEepromResourceBytes = 4080U;
constexpr std::string_view kFileStagingName = "staging.bin";
constexpr std::string_view kEepromStagingName = "eeprom.bin";

std::filesystem::path g_staging_root;  // Set once in main() before sessions.
std::mutex g_log_output_mutex;

void WriteGatewayLog(std::string_view entry) {
  std::lock_guard lock(g_log_output_mutex);
  std::fwrite(entry.data(), 1U, entry.size(), stderr);
  std::fputc('\n', stderr);
  std::fflush(stderr);
}

// Runtime metrics exported over upstream pw.metric.proto.MetricService on
// authenticated northbound client sessions. All PW_METRIC counters use
// pw::metric::TypedMetric<uint32_t> backed by lock-free std::atomic<uint32_t>
// saturating arithmetic, and the intrusive list topology is immutable after
// construction before accepting connections.
struct GatewayMetrics {
  GatewayMetrics() { root_groups.push_front(gateway_group); }
  GatewayMetrics(const GatewayMetrics&) = delete;
  GatewayMetrics& operator=(const GatewayMetrics&) = delete;

  pw::metric::MetricList root_metrics;
  pw::metric::GroupList root_groups;
  PW_METRIC_GROUP(gateway_group, "gateway");
  PW_METRIC(gateway_group, device_sessions_opened, "device_sessions_opened",
            0u);
  PW_METRIC(gateway_group, client_sessions_opened, "client_sessions_opened",
            0u);
  PW_METRIC(gateway_group, active_device_sessions, "active_device_sessions",
            0u);
  PW_METRIC(gateway_group, active_client_sessions, "active_client_sessions",
            0u);
  PW_METRIC(gateway_group, dispatches_total, "dispatches_total", 0u);
  PW_METRIC(gateway_group, dispatches_succeeded, "dispatches_succeeded", 0u);
  PW_METRIC(gateway_group, dispatches_failed, "dispatches_failed", 0u);
  PW_METRIC(gateway_group, telemetry_reports_total, "telemetry_reports_total",
            0u);
  PW_METRIC(gateway_group, client_rejections_total, "client_rejections_total",
            0u);
  PW_METRIC(gateway_group, acl_denials_total, "acl_denials_total", 0u);
};

std::filesystem::path ClientStagingDir(const std::string& client_id) {
  PW_CHECK(!g_staging_root.empty());
  return g_staging_root / "clients" / client_id;
}

std::filesystem::path DeviceStagingDir(const std::string& device_id) {
  PW_CHECK(!g_staging_root.empty());
  return g_staging_root / "devices" / device_id;
}

// Creates `dir` if needed and verifies it is a real directory owned by this
// process with mode 0700; refuses symlinks and foreign owners.
pw::Status EnsurePrivateDirectory(const std::filesystem::path& dir,
                                  std::string& error) {
  std::error_code ec;
  std::filesystem::create_directories(dir, ec);
  struct stat info{};
  if (lstat(dir.c_str(), &info) != 0 || !S_ISDIR(info.st_mode)) {
    error =
        "staging path is not a directory (or is a symlink): " + dir.string();
    return pw::Status::FailedPrecondition();
  }
  if (info.st_uid != geteuid()) {
    error = "staging directory is owned by another user: " + dir.string();
    return pw::Status::PermissionDenied();
  }
  if (chmod(dir.c_str(), S_IRWXU) != 0) {
    error = "cannot set mode 0700 on staging directory: " + dir.string();
    return pw::Status::Internal();
  }
  return pw::OkStatus();
}

pw::Status PrepareStagingRoot(const std::filesystem::path& root,
                              std::string& error) {
  PW_TRY(EnsurePrivateDirectory(root, error));
  PW_TRY(EnsurePrivateDirectory(root / "clients", error));
  PW_TRY(EnsurePrivateDirectory(root / "devices", error));
  return pw::OkStatus();
}

void RemoveStagingDirectory(const std::filesystem::path& dir) {
  std::error_code ec;
  std::filesystem::remove_all(dir, ec);
}

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

pw::Result<PeerIdentity> PeerIdentityFromCertificate(SSL* ssl) {
  if (SSL_get_verify_result(ssl) != X509_V_OK) {
    return pw::Status::Unauthenticated();
  }
  X509* certificate = SSL_get1_peer_certificate(ssl);
  if (certificate == nullptr) {
    return pw::Status::Unauthenticated();
  }
  auto* names = static_cast<GENERAL_NAMES*>(
      X509_get_ext_d2i(certificate, NID_subject_alt_name, nullptr, nullptr));
  std::optional<PeerIdentity> identity;
  size_t uri_san_count = 0;
  if (names != nullptr) {
    for (int i = 0; i < sk_GENERAL_NAME_num(names); ++i) {
      const GENERAL_NAME* name = sk_GENERAL_NAME_value(names, i);
      if (name == nullptr || name->type != GEN_URI) continue;
      if (++uri_san_count != 1U) {
        identity.reset();
        break;
      }
      const ASN1_IA5STRING* uri = name->d.uniformResourceIdentifier;
      const unsigned char* raw = ASN1_STRING_get0_data(uri);
      const int raw_size = ASN1_STRING_length(uri);
      if (raw == nullptr || raw_size <= 0) continue;
      const std::string_view value(reinterpret_cast<const char*>(raw),
                                   static_cast<size_t>(raw_size));
      PeerRole role;
      std::string_view prefix;
      if (value.compare(0, kDeviceIdentityPrefix.size(),
                        kDeviceIdentityPrefix) == 0) {
        role = PeerRole::kDevice;
        prefix = kDeviceIdentityPrefix;
      } else if (value.compare(0, kClientIdentityPrefix.size(),
                               kClientIdentityPrefix) == 0) {
        role = PeerRole::kClient;
        prefix = kClientIdentityPrefix;
      } else {
        continue;
      }
      if (identity.has_value()) {
        identity.reset();
        break;
      }
      const std::string_view principal_id = value.substr(prefix.size());
      if (!IsValidPrincipalId(principal_id)) {
        identity.reset();
        break;
      }
      identity = PeerIdentity{role, std::string(principal_id)};
    }
  }
  GENERAL_NAMES_free(names);
  X509_free(certificate);
  if (uri_san_count != 1U || !identity.has_value()) {
    return pw::Status::InvalidArgument();
  }
  return std::move(*identity);
}

class TlsSocket {
 public:
  TlsSocket(SSL* ssl, int fd) : ssl_(ssl), fd_(fd) {
    PW_CHECK_NOTNULL(ssl_);
    PW_CHECK_INT_GE(fd_, 0);
  }
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
      if (ssl_error == SSL_ERROR_WANT_READ) {
        if (!Wait(POLLIN, stopping)) return false;
      } else if (ssl_error == SSL_ERROR_WANT_WRITE) {
        if (!Wait(POLLOUT, stopping)) return false;
      } else {
        return false;
      }
    }
    return false;
  }
  bool Write(const std::byte* source, size_t size,
             const std::atomic<bool>& stopping) {
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
      } else {
        return false;
      }
    }
    return offset == size;
  }
  void Stop() const { shutdown(fd_, SHUT_RDWR); }
  ~TlsSocket() {
    if (ssl_ != nullptr) {
      SSL_shutdown(ssl_);
      SSL_free(ssl_);
    }
    if (fd_ >= 0) close(fd_);
  }

 private:
  bool HasPending() {
    std::lock_guard lock(ssl_mutex_);
    return SSL_pending(ssl_) > 0;
  }
  bool Wait(short events, const std::atomic<bool>& stopping) {
    pollfd descriptor{fd_, events, 0};
    while (!stopping.load()) {
      if ((events & POLLIN) != 0 && HasPending()) return true;
      const int result = poll(&descriptor, 1, 250);
      if (result > 0) {
        if ((descriptor.revents & (POLLERR | POLLHUP | POLLNVAL)) != 0)
          return false;
        if ((descriptor.revents & events) != 0) return true;
      } else if (result < 0 && errno != EINTR) {
        return false;
      }
    }
    return false;
  }
  SSL* ssl_;
  int fd_;
  std::mutex ssl_mutex_;
};

class TlsQueueWriter final : public pw::stream::NonSeekableWriter {
 public:
  explicit TlsQueueWriter(size_t capacity) : capacity_(capacity) {
    PW_CHECK_UINT_GT(capacity_, 0U);
  }
  size_t AvailableWriteBytes() const {
    std::lock_guard lock(mutex_);
    return capacity_ - queued_bytes_;
  }
  bool Pop(std::vector<std::byte>& output, const std::atomic<bool>& stopping) {
    while (true) {
      {
        std::lock_guard lock(mutex_);
        if (!queue_.empty()) {
          output = std::move(queue_.front());
          queue_.pop_front();
          queued_bytes_ -= output.size();
          return true;
        }
        if (stopped_ || stopping.load()) {
          return false;
        }
      }
      if (!ready_.try_acquire_for(pw::chrono::SystemClock::for_at_least(
              std::chrono::milliseconds(250)))) {
        return false;
      }
    }
  }
  void Stop() {
    {
      std::lock_guard lock(mutex_);
      stopped_ = true;
    }
    ready_.release();
  }

 protected:
  size_t ConservativeLimit(LimitType type) const override {
    return type == LimitType::kWrite ? AvailableWriteBytes() : 0U;
  }

 private:
  pw::Status DoWrite(pw::ConstByteSpan data) override {
    {
      std::lock_guard lock(mutex_);
      if (stopped_) return pw::Status::FailedPrecondition();
      if (data.size_bytes() > capacity_ - queued_bytes_)
        return pw::Status::ResourceExhausted();
      queue_.emplace_back(data.begin(), data.end());
      queued_bytes_ += data.size_bytes();
    }
    ready_.release();
    return pw::OkStatus();
  }
  const size_t capacity_;
  mutable std::mutex mutex_;
  pw::sync::TimedThreadNotification ready_;
  std::deque<std::vector<std::byte>> queue_;
  size_t queued_bytes_{0};
  bool stopped_{false};
};

// Thread-safe wrapper over upstream pw::hdlc::RpcChannelOutput (which is
// explicitly single-threaded). Serializes whole HDLC frames into TlsQueueWriter
// and emits structured gateway backpressure/queue logs.
class HdlcChannelOutput final : public pw::hdlc::RpcChannelOutput {
 public:
  HdlcChannelOutput(TlsQueueWriter& writer, uint64_t address)
      : pw::hdlc::RpcChannelOutput(writer, address, "mcubridge-tls-hdlc"),
        address_(address) {}

  pw::Status Send(pw::span<const std::byte> packet) override {
    std::lock_guard lock(mutex_);
    const pw::Status status = pw::hdlc::RpcChannelOutput::Send(packet);
    if (status == pw::Status::ResourceExhausted()) {
      PW_LOG_WARN(
          "event=rpc_frame_queue_rejected frame_bytes=%zu frame_size=%zu",
          packet.size(), pw::hdlc::MaxEncodedFrameSize(address_, packet));
    } else if (!status.ok()) {
      PW_LOG_WARN("event=rpc_frame_queue_failed frame_bytes=%zu status=%s",
                  packet.size(), status.str());
    } else {
      PW_LOG_INFO("event=rpc_frame_queued frame_bytes=%zu", packet.size());
    }
    return status;
  }

 private:
  const uint64_t address_;
  std::mutex mutex_;
};

class GatewaySession;

class DeviceSessionRegistry {
 public:
  bool Register(const std::string& device_id,
                std::shared_ptr<GatewaySession> session) {
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

// Client -> device authorization (ADR-0007). Deny by default: a dispatch is
// allowed only when a rule names the client and matches the device and the
// operation. "*" matches any device or operation as a whole field; there is
// no partial wildcard. Rules are loaded once at startup and never mutated.
class GatewayAcl {
 public:
  struct Rule {
    std::string client;
    std::string device;
    std::string operation;
  };

  static pw::Result<GatewayAcl> Load(const std::string& path,
                                     std::string& error) {
    GatewayAcl acl;
    if (path.empty()) return acl;
    std::ifstream input(path);
    if (!input) {
      error = "cannot read ACL file " + path;
      return pw::Status::NotFound();
    }
    std::string line;
    size_t line_number = 0;
    while (std::getline(input, line)) {
      ++line_number;
      const auto comment = line.find('#');
      if (comment != std::string::npos) line.erase(comment);
      std::istringstream fields(line);
      Rule rule;
      std::string extra;
      if (!(fields >> rule.client)) continue;  // blank or comment-only line
      if (!(fields >> rule.device >> rule.operation) || (fields >> extra) ||
          rule.client == "*") {
        error = "invalid ACL rule at " + path + ":" +
                std::to_string(line_number) +
                " (expected: <client> <device|*> <operation|*>)";
        return pw::Status::InvalidArgument();
      }
      acl.rules_.push_back(std::move(rule));
    }
    return acl;
  }

  bool Allows(std::string_view client, std::string_view device,
              std::string_view operation) const {
    for (const Rule& rule : rules_) {
      if (rule.client == client &&
          (rule.device == "*" || rule.device == device) &&
          (rule.operation == "*" || rule.operation == operation)) {
        return true;
      }
    }
    return false;
  }

  size_t size() const { return rules_.size(); }

 private:
  std::vector<Rule> rules_;
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
  DeviceTelemetryService(const std::string& device_id, GatewayMetrics& metrics)
      : device_id_(device_id), metrics_(metrics) {}

  pw::Status ReportTelemetry(
      const mcubridge::gateway::pwpb::TelemetryReport::Message& request,
      mcubridge::gateway::pwpb::TelemetryResponse::Message& response) {
    metrics_.telemetry_reports_total.Increment();
    PW_LOG_INFO(
        "device=%s event=telemetry_reported metrics_bytes=%zu status_bytes=%zu",
        device_id_.c_str(), request.daemon_metrics_blob.size(),
        request.system_status_blob.size());
    response.acknowledged = true;
    return pw::OkStatus();
  }

 private:
  const std::string& device_id_;
  GatewayMetrics& metrics_;
};

void LogHandlerRegistrationFailure(std::string_view role,
                                   const std::string& principal_id,
                                   const pw::transfer::Handler& handler) {
  PW_LOG_ERROR("%.*s=%s event=transfer_handler_registration_failed resource=%u",
               static_cast<int>(role.size()), role.data(), principal_id.c_str(),
               static_cast<unsigned>(handler.id()));
}

class BufferTransferHandler final : public pw::transfer::Handler {
 public:
  BufferTransferHandler(uint32_t resource_id, pw::ByteSpan storage)
      : Handler(resource_id, static_cast<pw::stream::Reader*>(nullptr)),
        storage_(storage) {}

  pw::Status PrepareRead() override {
    reader_.emplace(pw::ConstByteSpan(storage_.data(), valid_bytes_));
    set_reader(*reader_);
    return pw::OkStatus();
  }

  pw::Status PrepareWrite() override {
    writer_.emplace(storage_);
    set_writer(*writer_);
    return pw::OkStatus();
  }

  pw::Status FinalizeWrite(pw::Status status) override {
    if (status.ok() && writer_.has_value()) {
      valid_bytes_ = writer_->bytes_written();
    }
    return status;
  }

  size_t ResourceSize() const override { return valid_bytes_; }

 private:
  pw::ByteSpan storage_;
  size_t valid_bytes_{0};
  std::optional<pw::stream::MemoryReader> reader_;
  std::optional<pw::stream::MemoryWriter> writer_;
};

pw::Result<std::vector<std::byte>> ReadBoundedFile(
    const std::filesystem::path& target_path, size_t max_bytes) {
  std::error_code ec;
  if (!std::filesystem::is_regular_file(target_path, ec)) {
    return pw::Status::NotFound();
  }
  const auto file_size = std::filesystem::file_size(target_path, ec);
  if (ec || file_size > max_bytes) {
    return pw::Status::ResourceExhausted();
  }
  std::vector<std::byte> bytes(file_size);
  const int fd = open(target_path.c_str(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
  if (fd < 0) return pw::Status::Internal();
  size_t total_read = 0;
  while (total_read < file_size) {
    const ssize_t rc =
        read(fd, bytes.data() + total_read, file_size - total_read);
    if (rc <= 0) {
      close(fd);
      return pw::Status::Internal();
    }
    total_read += static_cast<size_t>(rc);
  }
  close(fd);
  return bytes;
}

pw::Status WriteAtomicFile(const std::filesystem::path& root_dir,
                           const std::string& filename,
                           pw::ConstByteSpan data) {
  const auto tmp_path = root_dir / ("." + filename + ".tmp");
  const auto target_path = root_dir / filename;
  const int fd =
      open(tmp_path.c_str(),
           O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW | O_CLOEXEC, 0600);
  if (fd < 0) {
    return pw::Status::Internal();
  }
  size_t total_written = 0;
  while (total_written < data.size()) {
    const ssize_t rc =
        write(fd, data.data() + total_written, data.size() - total_written);
    if (rc <= 0) {
      close(fd);
      unlink(tmp_path.c_str());
      return pw::Status::Internal();
    }
    total_written += static_cast<size_t>(rc);
  }
  close(fd);
  if (rename(tmp_path.c_str(), target_path.c_str()) != 0) {
    unlink(tmp_path.c_str());
    return pw::Status::Internal();
  }
  return pw::OkStatus();
}

class BoundedFileTransferHandler final : public pw::transfer::Handler {
 public:
  BoundedFileTransferHandler(uint32_t resource_id,
                             std::filesystem::path root_dir,
                             std::string filename,
                             size_t max_bytes = 1024 * 1024)
      : Handler(resource_id, static_cast<pw::stream::Reader*>(nullptr)),
        root_dir_(std::move(root_dir)),
        filename_(std::move(filename)),
        max_bytes_(max_bytes) {}

  ~BoundedFileTransferHandler() override { CloseFiles(); }

  pw::Status PrepareRead() override {
    CloseFiles();
    PW_TRY(ValidateFilename());
    PW_TRY_ASSIGN(file_bytes_,
                  ReadBoundedFile(root_dir_ / filename_, max_bytes_));
    reader_.emplace(pw::ConstByteSpan(file_bytes_));
    set_reader(*reader_);
    return pw::OkStatus();
  }

  void FinalizeRead(pw::Status) override { CloseFiles(); }

  pw::Status PrepareWrite() override {
    CloseFiles();
    PW_TRY(ValidateFilename());
    std::error_code ec;
    std::filesystem::create_directories(root_dir_, ec);
    std::filesystem::permissions(root_dir_,
                                 std::filesystem::perms::owner_read |
                                     std::filesystem::perms::owner_write |
                                     std::filesystem::perms::owner_exec,
                                 std::filesystem::perm_options::replace, ec);
    write_buffer_.resize(max_bytes_);
    writer_.emplace(pw::ByteSpan(write_buffer_));
    set_writer(*writer_);
    return pw::OkStatus();
  }

  pw::Status FinalizeWrite(pw::Status status) override {
    struct ScopeClose {
      BoundedFileTransferHandler& self;
      ~ScopeClose() { self.CloseFiles(); }
    } close_guard{*this};

    PW_TRY(status);
    if (!writer_.has_value()) {
      return pw::Status::FailedPrecondition();
    }
    const size_t written = writer_->bytes_written();
    if (written > max_bytes_) {
      return pw::Status::ResourceExhausted();
    }
    return WriteAtomicFile(root_dir_, filename_,
                           pw::ConstByteSpan(write_buffer_.data(), written));
  }

  size_t ResourceSize() const override {
    std::error_code ec;
    const auto target_path = root_dir_ / filename_;
    if (std::filesystem::is_regular_file(target_path, ec)) {
      return std::filesystem::file_size(target_path, ec);
    }
    return 0;
  }

 private:
  pw::Status ValidateFilename() const {
    if (filename_.empty() || filename_.find("..") != std::string::npos ||
        filename_.find('/') != std::string::npos ||
        filename_.find('\\') != std::string::npos) {
      return pw::Status::PermissionDenied();
    }
    return pw::OkStatus();
  }

  void CloseFiles() {
    reader_.reset();
    writer_.reset();
    file_bytes_.clear();
    file_bytes_.shrink_to_fit();
    write_buffer_.clear();
    write_buffer_.shrink_to_fit();
  }

  std::filesystem::path root_dir_;
  std::string filename_;
  size_t max_bytes_;
  std::vector<std::byte> file_bytes_;
  std::vector<std::byte> write_buffer_;
  std::optional<pw::stream::MemoryReader> reader_;
  std::optional<pw::stream::MemoryWriter> writer_;
};

class GatewaySession final
    : public SessionControl,
      public std::enable_shared_from_this<GatewaySession> {
 public:
  GatewaySession(SSL* ssl, int fd, std::string device_id,
                 DeviceSessionRegistry& registry, GatewayMetrics& metrics)
      : socket_(ssl, fd),
        registry_(registry),
        metrics_(metrics),
        channel_output_(tx_writer_, kRpcHdlcAddress),
        client_channels_{pw::rpc::Channel::Create<1>(&channel_output_)},
        server_channels_{pw::rpc::Channel::Create<1>(&channel_output_)},
        rpc_client_(client_channels_),
        rpc_server_(server_channels_),
        transfer_thread_(pw::ByteSpan(transfer_chunk_),
                         pw::ByteSpan(transfer_encode_)),
        transfer_service_(transfer_thread_, kTransferWindowBytes),
        device_id_(std::move(device_id)),
        telemetry_service_(device_id_, metrics_),
        edge_control_client_(rpc_client_, 1),
        decoder_(pw::ByteSpan(decoder_storage_)) {}
  GatewaySession(const GatewaySession&) = delete;
  GatewaySession& operator=(const GatewaySession&) = delete;

  void Run() override {
    if (!socket_.SetNonBlocking()) return;
    if (!registry_.Register(device_id_, shared_from_this())) {
      metrics_.client_rejections_total.Increment();
      PW_LOG_WARN("device=%s event=session_rejected reason=duplicate_session",
                  device_id_.c_str());
      return;
    }
    metrics_.device_sessions_opened.Increment();
    metrics_.active_device_sessions.Increment();
    struct RegistrationGuard {
      DeviceSessionRegistry& registry;
      GatewayMetrics& metrics;
      const std::string& device_id;
      const GatewaySession* session;
      ~RegistrationGuard() {
        registry.Unregister(device_id, session);
        metrics.active_device_sessions.Decrement();
        PW_LOG_INFO("device=%s event=session_unregistered", device_id.c_str());
      }
    } registration{registry_, metrics_, device_id_, this};

    if (stopping_.load()) return;

    // The transfer thread must run before handlers are registered:
    // TransferService::RegisterHandler() posts an event that only the running
    // thread can process, and returns false after the event-processing timeout
    // when the thread has not started, leaving the resource unregistered.
    pw::thread::stl::Options thread_options;
    transfer_worker_ =
        pw::thread::Thread(thread_options, transfer_thread_.AsFunction());

    rpc_server_.RegisterService(telemetry_service_, transfer_service_);
    services_registered_.store(true);
    if (transfer_service_.RegisterHandler(default_transfer_handler_)) {
      default_handler_registered_.store(true);
    } else {
      LogHandlerRegistrationFailure("device", device_id_,
                                    default_transfer_handler_);
    }
    if (transfer_service_.RegisterHandler(file_transfer_handler_)) {
      file_handler_registered_.store(true);
    } else {
      LogHandlerRegistrationFailure("device", device_id_,
                                    file_transfer_handler_);
    }
    if (transfer_service_.RegisterHandler(eeprom_transfer_handler_)) {
      eeprom_handler_registered_.store(true);
    } else {
      LogHandlerRegistrationFailure("device", device_id_,
                                    eeprom_transfer_handler_);
    }

    tx_worker_ = pw::thread::Thread(thread_options, [this] { TransmitLoop(); });
    PW_LOG_INFO("device=%s event=session_open", device_id_.c_str());
    ReceiveLoop();
    stopping_.store(true);
    dispatch_done_.release();
    socket_.Stop();
    tx_writer_.Stop();
    if (tx_worker_.joinable()) tx_worker_.join();
    TerminateTransfer();
    UnregisterServices();
    RemoveStagingDirectory(DeviceStagingDir(device_id_));
    PW_LOG_INFO("device=%s event=session_closed", device_id_.c_str());
  }

  void Stop() override {
    stopping_.store(true);
    dispatch_done_.release();
    socket_.Stop();
    tx_writer_.Stop();
  }

  ~GatewaySession() override {
    stopping_.store(true);
    dispatch_done_.release();
    TerminateTransfer();
    UnregisterServices();
    if (tx_worker_.joinable()) tx_worker_.join();
  }

  pw::Status Dispatch(
      const mcubridge::gateway::pwpb::DispatchRequest::Message& request,
      mcubridge::gateway::pwpb::DispatchResponse::Message& response) {
    if (stopping_.load()) {
      return pw::Status::Unavailable();
    }

    std::lock_guard session_lock(dispatch_mutex_);
    // Drain any stale wakeup before re-checking stopping_ so a concurrent
    // Stop() or disconnect cannot race past our wait loop.
    static_cast<void>(dispatch_done_.try_acquire());
    if (stopping_.load()) {
      return pw::Status::Unavailable();
    }

    mcubridge::gateway::pwpb::EdgeDispatchRequest::Message edge_req{};
    edge_req.operation = request.operation;
    edge_req.request_payload = request.request_payload;
    edge_req.timeout_ms = request.timeout_ms;

    struct DispatchContext {
      explicit DispatchContext(pw::sync::TimedThreadNotification& notification)
          : done(notification) {}
      pw::sync::TimedThreadNotification& done;
      std::atomic<bool> completed{false};
      pw::Status call_status = pw::OkStatus();
      uint32_t status_code = 0;
      std::vector<std::byte> response_payload;
    } ctx(dispatch_done_);

    const uint32_t timeout =
        request.timeout_ms > 0 ? request.timeout_ms : 30000U;
    const auto deadline = pw::chrono::SystemClock::TimePointAfterAtLeast(
        pw::chrono::SystemClock::for_at_least(
            std::chrono::milliseconds(timeout)));

    {
      auto call = edge_control_client_.Dispatch(
          edge_req,
          [&ctx](const mcubridge::gateway::pwpb::EdgeDispatchResponse::Message&
                     edge_resp,
                 pw::Status status) {
            ctx.call_status = status;
            ctx.status_code = edge_resp.status_code;
            ctx.response_payload.assign(edge_resp.response_payload.begin(),
                                        edge_resp.response_payload.end());
            ctx.completed.store(true, std::memory_order_release);
            PW_LOG_INFO(
                "event=edge_dispatch_response_received status=%s "
                "device_status=%u payload_bytes=%zu",
                status.str(), static_cast<unsigned>(edge_resp.status_code),
                edge_resp.response_payload.size());
            ctx.done.release();
          },
          [&ctx](pw::Status status) {
            ctx.call_status = status;
            ctx.completed.store(true, std::memory_order_release);
            PW_LOG_WARN("event=edge_dispatch_rpc_error status=%s",
                        status.str());
            ctx.done.release();
          });

      if (!call.active()) {
        PW_LOG_WARN("event=edge_dispatch_call_inactive");
        return pw::Status::Unavailable();
      }
      PW_LOG_INFO("event=edge_dispatch_call_started timeout_ms=%u",
                  static_cast<unsigned>(timeout));

      while (!ctx.completed.load(std::memory_order_acquire) &&
             !stopping_.load()) {
        if (!dispatch_done_.try_acquire_until(deadline)) {
          break;
        }
      }

      if (!ctx.completed.load(std::memory_order_acquire)) {
        call.Cancel().IgnoreError();
      }
    }  // Destroys `call` and waits for any in-flight RPC callback to finish.

    if (!ctx.completed.load(std::memory_order_acquire)) {
      return stopping_.load() ? pw::Status::Unavailable()
                              : pw::Status::DeadlineExceeded();
    }

    PW_TRY(ctx.call_status);
    response.status_code = ctx.status_code;
    response.response_payload.assign(ctx.response_payload.begin(),
                                     ctx.response_payload.end());
    return pw::OkStatus();
  }

  std::mutex& staging_mutex() { return staging_mutex_; }

 private:
  void UnregisterTransferHandlers() {
    if (default_handler_registered_.load()) {
      transfer_service_.UnregisterHandler(default_transfer_handler_);
      default_handler_registered_.store(false);
    }
    if (file_handler_registered_.load()) {
      transfer_service_.UnregisterHandler(file_transfer_handler_);
      file_handler_registered_.store(false);
    }
    if (eeprom_handler_registered_.load()) {
      transfer_service_.UnregisterHandler(eeprom_transfer_handler_);
      eeprom_handler_registered_.store(false);
    }
  }

  void TerminateTransfer() {
    bool expected = false;
    if (transfer_terminated_.compare_exchange_strong(expected, true)) {
      if (transfer_worker_.joinable()) {
        UnregisterTransferHandlers();
        transfer_thread_.Terminate();
        transfer_worker_.join();
      }
    }
  }

  void UnregisterServices() {
    bool expected = false;
    if (services_unregistered_.compare_exchange_strong(expected, true)) {
      if (services_registered_.load()) {
        rpc_server_.UnregisterService(telemetry_service_, transfer_service_);
        services_registered_.store(false);
      }
    }
  }

  void TransmitLoop() {
    std::vector<std::byte> frame;
    while (!stopping_.load()) {
      if (!tx_writer_.Pop(frame, stopping_)) continue;
      if (!socket_.Write(frame.data(), frame.size(), stopping_)) {
        stopping_.store(true);
        dispatch_done_.release();
        socket_.Stop();
        break;
      }
    }
  }

  pw::Status ProcessDevicePacket(pw::ConstByteSpan data) {
    const pw::Result<pw::rpc::PacketMeta> meta =
        pw::rpc::PacketMeta::FromBuffer(data);
    if (!meta.ok()) {
      PW_LOG_WARN("device=%s event=rpc_packet_corrupt", device_id_.c_str());
      return pw::OkStatus();
    }
    if (meta->destination_is_client()) {
      const pw::Status status = rpc_client_.ProcessPacket(data);
      if (!status.ok() && status != pw::Status::Unavailable()) {
        PW_LOG_WARN("device=%s event=rpc_client_packet_rejected status=%s",
                    device_id_.c_str(), status.str());
      }
      return status;
    }
    if (meta->destination_is_server()) {
      return rpc_server_.ProcessPacket(data);
    }
    return pw::Status::InvalidArgument();
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
        const pw::Status status = ProcessDevicePacket(frame->data());
        if (!status.ok() && status != pw::Status::Unavailable()) {
          PW_LOG_WARN("device=%s event=rpc_packet_rejected status=%u",
                      device_id_.c_str(), static_cast<unsigned>(status.code()));
        }
      }
    }
  }

  TlsSocket socket_;
  DeviceSessionRegistry& registry_;
  GatewayMetrics& metrics_;
  TlsQueueWriter tx_writer_{kTxQueueBytes};
  HdlcChannelOutput channel_output_;
  std::array<pw::rpc::Channel, 1> client_channels_;
  std::array<pw::rpc::Channel, 1> server_channels_;
  pw::rpc::Client rpc_client_;
  pw::rpc::Server rpc_server_;
  std::array<std::byte, kTransferChunkBytes> transfer_chunk_{};
  std::array<std::byte, kRpcEncodeBytes> transfer_encode_{};
  pw::transfer::Thread<1, 1> transfer_thread_;
  pw::transfer::TransferService transfer_service_;
  pw::thread::Thread transfer_worker_;
  std::string device_id_;
  std::array<std::byte, 64 * 1024> device_transfer_buffer_{};
  BufferTransferHandler default_transfer_handler_{
      kDefaultTransferResource, pw::ByteSpan(device_transfer_buffer_)};
  BoundedFileTransferHandler file_transfer_handler_{
      kFileTransferResource, DeviceStagingDir(device_id_),
      std::string(kFileStagingName), kFileStagingMaxBytes};
  BoundedFileTransferHandler eeprom_transfer_handler_{
      kEepromTransferResource, DeviceStagingDir(device_id_),
      std::string(kEepromStagingName), kEepromResourceBytes};
  DeviceTelemetryService telemetry_service_;
  mcubridge::gateway::pw_rpc::pwpb::EdgeControl::Client edge_control_client_;
  std::array<std::byte, kHdlcDecodeBytes> decoder_storage_{};
  pw::hdlc::Decoder decoder_;
  std::atomic<bool> stopping_{false};
  std::atomic<bool> default_handler_registered_{false};
  std::atomic<bool> file_handler_registered_{false};
  std::atomic<bool> eeprom_handler_registered_{false};
  std::atomic<bool> transfer_terminated_{false};
  std::atomic<bool> services_registered_{false};
  std::atomic<bool> services_unregistered_{false};
  pw::sync::TimedThreadNotification dispatch_done_;
  pw::thread::Thread tx_worker_;
  std::mutex dispatch_mutex_;
  std::mutex staging_mutex_;
};

class GatewayControlService final
    : public mcubridge::gateway::pw_rpc::pwpb::GatewayControl::Service<
          GatewayControlService> {
 public:
  GatewayControlService(DeviceSessionRegistry& devices,
                        ClientSessionRegistry& clients, const GatewayAcl& acl,
                        GatewayMetrics& metrics, std::string client_id)
      : devices_(devices),
        clients_(clients),
        acl_(acl),
        metrics_(metrics),
        client_id_(std::move(client_id)) {}

  pw::Status Health(
      const mcubridge::gateway::pwpb::HealthRequest::Message&,
      mcubridge::gateway::pwpb::HealthResponse::Message& response) {
    response.ready = true;
    response.active_device_sessions = static_cast<uint32_t>(devices_.size());
    response.active_client_sessions = static_cast<uint32_t>(clients_.size());
    return pw::OkStatus();
  }

  pw::Status Dispatch(
      const mcubridge::gateway::pwpb::DispatchRequest::Message& request,
      mcubridge::gateway::pwpb::DispatchResponse::Message& response) {
    metrics_.dispatches_total.Increment();
    const pw::Status status = ExecuteDispatch(request, response);
    if (status.ok()) {
      metrics_.dispatches_succeeded.Increment();
    } else {
      metrics_.dispatches_failed.Increment();
    }
    return status;
  }

 private:
  pw::Status StageClientFileToDevice(const std::filesystem::path& client_file,
                                     const std::filesystem::path& device_file,
                                     const std::string& target_id) const {
    std::error_code ec;
    if (!std::filesystem::is_regular_file(client_file, ec)) {
      PW_LOG_WARN("client=%s event=dispatch_missing_staging target=%s",
                  client_id_.c_str(), target_id.c_str());
      return pw::Status::FailedPrecondition();
    }
    std::string error;
    if (!EnsurePrivateDirectory(DeviceStagingDir(target_id), error).ok()) {
      PW_LOG_ERROR("client=%s event=dispatch_staging_error target=%s detail=%s",
                   client_id_.c_str(), target_id.c_str(), error.c_str());
      return pw::Status::Internal();
    }
    std::filesystem::rename(client_file, device_file, ec);
    if (ec) {
      PW_LOG_ERROR("client=%s event=dispatch_staging_error target=%s",
                   client_id_.c_str(), target_id.c_str());
      return pw::Status::Internal();
    }
    return pw::OkStatus();
  }

  pw::Status StageDeviceFileToClient(const std::filesystem::path& device_file,
                                     const std::filesystem::path& client_file,
                                     const std::string& target_id) const {
    std::error_code ec;
    if (!std::filesystem::is_regular_file(device_file, ec)) {
      PW_LOG_WARN("client=%s event=dispatch_missing_staging target=%s",
                  client_id_.c_str(), target_id.c_str());
      return pw::Status::NotFound();
    }
    std::string error;
    if (!EnsurePrivateDirectory(ClientStagingDir(client_id_), error).ok()) {
      std::filesystem::remove(device_file, ec);
      return pw::Status::Internal();
    }
    std::filesystem::rename(device_file, client_file, ec);
    if (ec) {
      std::filesystem::remove(device_file, ec);
      return pw::Status::Internal();
    }
    return pw::OkStatus();
  }

  pw::Status ExecuteDispatch(
      const mcubridge::gateway::pwpb::DispatchRequest::Message& request,
      mcubridge::gateway::pwpb::DispatchResponse::Message& response) {
    const std::string target_id(request.target_device_id);
    const std::string_view op(request.operation);
    // Checked before the device lookup so a denied client cannot probe which
    // devices are connected.
    if (!acl_.Allows(client_id_, target_id, op)) {
      metrics_.acl_denials_total.Increment();
      PW_LOG_WARN("client=%s event=dispatch_denied target=%s operation=%.*s",
                  client_id_.c_str(), target_id.c_str(),
                  static_cast<int>(op.size()), op.data());
      return pw::Status::PermissionDenied();
    }
    auto session = devices_.Lookup(target_id);
    if (!session) {
      return pw::Status::NotFound();
    }

    // Operations that move a staged file between the client's and the
    // device's private directories (ADR-0007). Push consumes the client's
    // copy; the device copy is removed once the daemon has read it. Fetch
    // hands the daemon's copy to the client.
    struct StagedOperation {
      std::string_view operation;
      std::string_view filename;
      bool to_device;
    };
    static constexpr std::array<StagedOperation, 4> kStagedOperations{{
        {"rpc.pb.FileTransferService/PushToLinux", kFileStagingName, true},
        {"rpc.pb.FileTransferService/FetchFromLinux", kFileStagingName, false},
        // ADR-0007: the EEPROM image travels through eeprom.bin.
        {"rpc.pb.EepromService/Write", kEepromStagingName, true},
        {"rpc.pb.EepromService/Read", kEepromStagingName, false},
    }};
    const StagedOperation* staged = nullptr;
    for (const auto& candidate : kStagedOperations) {
      if (candidate.operation == op) staged = &candidate;
    }
    const auto client_file =
        staged ? ClientStagingDir(client_id_) / staged->filename
               : std::filesystem::path();
    const auto device_file =
        staged ? DeviceStagingDir(target_id) / staged->filename
               : std::filesystem::path();
    std::error_code ec;

    // Serialize staged file moves and the device dispatch per target device so
    // concurrent clients cannot overwrite or consume each other's staging file.
    std::unique_lock<std::mutex> staging_lock;
    if (staged != nullptr) {
      staging_lock = std::unique_lock<std::mutex>(session->staging_mutex());
    }

    if (staged && staged->to_device) {
      PW_TRY(StageClientFileToDevice(client_file, device_file, target_id));
    }

    const pw::Status status = session->Dispatch(request, response);
    PW_LOG_INFO(
        "client=%s event=dispatch_completed target=%s operation=%.*s status=%s "
        "device_status=%u",
        client_id_.c_str(), target_id.c_str(), static_cast<int>(op.size()),
        op.data(), status.str(), static_cast<unsigned>(response.status_code));

    if (staged && staged->to_device) {
      std::filesystem::remove(device_file, ec);
    } else if (staged && status.ok()) {
      PW_TRY(StageDeviceFileToClient(device_file, client_file, target_id));
    }

    return status;
  }

  DeviceSessionRegistry& devices_;
  ClientSessionRegistry& clients_;
  const GatewayAcl& acl_;
  GatewayMetrics& metrics_;
  std::string client_id_;
};

class GatewayClientSession final : public SessionControl {
 public:
  GatewayClientSession(SSL* ssl, int fd, std::string client_id,
                       DeviceSessionRegistry& devices,
                       ClientSessionRegistry& clients, const GatewayAcl& acl,
                       GatewayMetrics& metrics)
      : socket_(ssl, fd),
        clients_(clients),
        metrics_(metrics),
        client_id_(std::move(client_id)),
        control_service_(devices, clients, acl, metrics, client_id_),
        metric_service_(metrics.root_metrics, metrics.root_groups),
        channel_output_(tx_writer_, kRpcHdlcAddress),
        channels_{pw::rpc::Channel::Create<1>(&channel_output_)},
        rpc_server_(channels_),
        transfer_thread_(pw::ByteSpan(transfer_chunk_),
                         pw::ByteSpan(transfer_encode_)),
        transfer_service_(transfer_thread_, kTransferWindowBytes),
        decoder_(pw::ByteSpan(decoder_storage_)) {}
  GatewayClientSession(const GatewayClientSession&) = delete;
  GatewayClientSession& operator=(const GatewayClientSession&) = delete;

  void Run() override {
    if (!socket_.SetNonBlocking()) return;
    if (!clients_.Register(client_id_, this)) {
      metrics_.client_rejections_total.Increment();
      PW_LOG_WARN("client=%s event=session_rejected reason=duplicate_session",
                  client_id_.c_str());
      return;
    }
    metrics_.client_sessions_opened.Increment();
    metrics_.active_client_sessions.Increment();
    struct RegistrationGuard {
      ClientSessionRegistry& registry;
      GatewayMetrics& metrics;
      const std::string& client_id;
      const void* session;
      ~RegistrationGuard() {
        registry.Unregister(client_id, session);
        metrics.active_client_sessions.Decrement();
        PW_LOG_INFO("client=%s event=session_unregistered", client_id.c_str());
      }
    } registration{clients_, metrics_, client_id_, this};

    if (stopping_.load()) return;

    pw::thread::stl::Options thread_options;
    transfer_worker_ =
        pw::thread::Thread(thread_options, transfer_thread_.AsFunction());

    rpc_server_.RegisterService(control_service_, transfer_service_,
                                metric_service_);
    services_registered_.store(true);
    if (transfer_service_.RegisterHandler(default_transfer_handler_)) {
      default_handler_registered_.store(true);
    } else {
      LogHandlerRegistrationFailure("client", client_id_,
                                    default_transfer_handler_);
    }
    if (transfer_service_.RegisterHandler(eeprom_transfer_handler_)) {
      eeprom_handler_registered_.store(true);
    } else {
      LogHandlerRegistrationFailure("client", client_id_,
                                    eeprom_transfer_handler_);
    }
    if (transfer_service_.RegisterHandler(file_transfer_handler_)) {
      file_handler_registered_.store(true);
    } else {
      LogHandlerRegistrationFailure("client", client_id_,
                                    file_transfer_handler_);
    }
    tx_worker_ = pw::thread::Thread(thread_options, [this] { TransmitLoop(); });
    PW_LOG_INFO("client=%s event=session_open", client_id_.c_str());
    ReceiveLoop();
    stopping_.store(true);
    socket_.Stop();
    tx_writer_.Stop();
    if (tx_worker_.joinable()) tx_worker_.join();
    TerminateTransfer();
    UnregisterServices();
    RemoveStagingDirectory(ClientStagingDir(client_id_));
    PW_LOG_INFO("client=%s event=session_closed", client_id_.c_str());
  }

  void Stop() override {
    stopping_.store(true);
    socket_.Stop();
    tx_writer_.Stop();
  }

  ~GatewayClientSession() override {
    stopping_.store(true);
    TerminateTransfer();
    UnregisterServices();
    if (tx_worker_.joinable()) tx_worker_.join();
  }

 private:
  void UnregisterTransferHandlers() {
    if (default_handler_registered_.load()) {
      transfer_service_.UnregisterHandler(default_transfer_handler_);
      default_handler_registered_.store(false);
    }
    if (eeprom_handler_registered_.load()) {
      transfer_service_.UnregisterHandler(eeprom_transfer_handler_);
      eeprom_handler_registered_.store(false);
    }
    if (file_handler_registered_.load()) {
      transfer_service_.UnregisterHandler(file_transfer_handler_);
      file_handler_registered_.store(false);
    }
  }

  void TerminateTransfer() {
    bool expected = false;
    if (transfer_terminated_.compare_exchange_strong(expected, true)) {
      if (transfer_worker_.joinable()) {
        UnregisterTransferHandlers();
        transfer_thread_.Terminate();
        transfer_worker_.join();
      }
    }
  }

  void UnregisterServices() {
    bool expected = false;
    if (services_unregistered_.compare_exchange_strong(expected, true)) {
      if (services_registered_.load()) {
        rpc_server_.UnregisterService(control_service_, transfer_service_,
                                      metric_service_);
        services_registered_.store(false);
      }
    }
  }

  void TransmitLoop() {
    std::vector<std::byte> frame;
    while (!stopping_.load()) {
      if (!tx_writer_.Pop(frame, stopping_)) continue;
      if (!socket_.Write(frame.data(), frame.size(), stopping_)) {
        PW_LOG_WARN("client=%s event=tx_frame_failed frame_bytes=%zu",
                    client_id_.c_str(), frame.size());
        stopping_.store(true);
        socket_.Stop();
        break;
      }
      PW_LOG_INFO("client=%s event=tx_frame_sent frame_bytes=%zu",
                  client_id_.c_str(), frame.size());
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
          PW_LOG_WARN("client=%s event=rpc_packet_rejected status=%u",
                      client_id_.c_str(), static_cast<unsigned>(status.code()));
        }
      }
    }
  }

  TlsSocket socket_;
  ClientSessionRegistry& clients_;
  GatewayMetrics& metrics_;
  std::string client_id_;
  GatewayControlService control_service_;
  pw::metric::MetricService metric_service_;
  TlsQueueWriter tx_writer_{kTxQueueBytes};
  HdlcChannelOutput channel_output_;
  std::array<pw::rpc::Channel, 1> channels_;
  pw::rpc::Server rpc_server_;
  std::array<std::byte, kTransferChunkBytes> transfer_chunk_{};
  std::array<std::byte, kRpcEncodeBytes> transfer_encode_{};
  pw::transfer::Thread<1, 1> transfer_thread_;
  pw::transfer::TransferService transfer_service_;
  pw::thread::Thread transfer_worker_;
  std::array<std::byte, 64 * 1024> client_transfer_buffer_{};
  BufferTransferHandler default_transfer_handler_{
      kDefaultTransferResource, pw::ByteSpan(client_transfer_buffer_)};
  BoundedFileTransferHandler eeprom_transfer_handler_{
      kEepromTransferResource, ClientStagingDir(client_id_),
      std::string(kEepromStagingName), kEepromResourceBytes};
  BoundedFileTransferHandler file_transfer_handler_{
      kFileTransferResource, ClientStagingDir(client_id_),
      std::string(kFileStagingName), kFileStagingMaxBytes};
  std::array<std::byte, kHdlcDecodeBytes> decoder_storage_{};
  pw::hdlc::Decoder decoder_;
  std::atomic<bool> stopping_{false};
  std::atomic<bool> transfer_terminated_{false};
  std::atomic<bool> services_registered_{false};
  std::atomic<bool> services_unregistered_{false};
  std::atomic<bool> default_handler_registered_{false};
  std::atomic<bool> eeprom_handler_registered_{false};
  std::atomic<bool> file_handler_registered_{false};
  pw::thread::Thread tx_worker_;
};

struct SessionTask {
  SessionTask(int client_fd, SSL_CTX* tls_context,
              DeviceSessionRegistry& device_registry,
              ClientSessionRegistry& client_registry, const GatewayAcl& acl,
              GatewayMetrics& metrics)
      : fd(client_fd),
        context(tls_context),
        devices(device_registry),
        clients(client_registry),
        acl_ref(acl),
        metrics_ref(metrics) {}

  void Run() {
    const auto close_task_fd = [this] {
      std::lock_guard lock(mutex);
      if (fd >= 0) {
        close(fd);
        fd = -1;
      }
    };
    int client_fd = -1;
    {
      std::lock_guard lock(mutex);
      client_fd = fd;
    }
    SSL* ssl = SSL_new(context);
    if (ssl == nullptr) {
      metrics_ref.client_rejections_total.Increment();
      close_task_fd();
      done.store(true);
      return;
    }
    SSL_set_fd(ssl, client_fd);
    timeval handshake_timeout{5, 0};
    setsockopt(client_fd, SOL_SOCKET, SO_RCVTIMEO, &handshake_timeout,
               sizeof(handshake_timeout));
    setsockopt(client_fd, SOL_SOCKET, SO_SNDTIMEO, &handshake_timeout,
               sizeof(handshake_timeout));
    SSL_set_mode(ssl, SSL_MODE_ENABLE_PARTIAL_WRITE |
                          SSL_MODE_ACCEPT_MOVING_WRITE_BUFFER);
    if (SSL_accept(ssl) != 1) {
      metrics_ref.client_rejections_total.Increment();
      PW_LOG_WARN("event=client_rejected reason=tls_handshake_failed");
      SSL_free(ssl);
      close_task_fd();
      done.store(true);
      return;
    }
    auto identity = PeerIdentityFromCertificate(ssl);
    if (!identity.ok()) {
      metrics_ref.client_rejections_total.Increment();
      PW_LOG_WARN("event=client_rejected reason=missing_or_invalid_uri_san");
      SSL_shutdown(ssl);
      SSL_free(ssl);
      close_task_fd();
      done.store(true);
      return;
    }
    std::shared_ptr<SessionControl> active_session;
    if (identity->role == PeerRole::kDevice) {
      active_session = std::make_shared<GatewaySession>(
          ssl, client_fd, std::move(identity->id), devices, metrics_ref);
    } else {
      active_session = std::make_shared<GatewayClientSession>(
          ssl, client_fd, std::move(identity->id), devices, clients, acl_ref,
          metrics_ref);
    }
    {
      std::lock_guard lock(mutex);
      session = active_session;
      fd = -1;
    }
    active_session->Run();
    done.store(true);
  }

  int fd{-1};
  SSL_CTX* context{nullptr};
  DeviceSessionRegistry& devices;
  ClientSessionRegistry& clients;
  const GatewayAcl& acl_ref;
  GatewayMetrics& metrics_ref;
  std::atomic<bool> done{false};
  std::mutex mutex;
  std::shared_ptr<SessionControl> session;
  pw::thread::Thread worker;
};

struct TlsContextDeleter {
  void operator()(SSL_CTX* context) const { SSL_CTX_free(context); }
};
using TlsContext = std::unique_ptr<SSL_CTX, TlsContextDeleter>;

pw::Result<TlsContext> CreateTlsContext(const std::string& certificate,
                                        const std::string& private_key,
                                        const std::string& client_ca,
                                        std::string& error) {
  TlsContext context(SSL_CTX_new(TLS_server_method()));
  if (!context) {
    error = "SSL_CTX_new failed";
    return pw::Status::Internal();
  }
  if (SSL_CTX_set_min_proto_version(context.get(), TLS1_3_VERSION) != 1 ||
      SSL_CTX_use_certificate_chain_file(context.get(), certificate.c_str()) !=
          1 ||
      SSL_CTX_use_PrivateKey_file(context.get(), private_key.c_str(),
                                  SSL_FILETYPE_PEM) != 1 ||
      SSL_CTX_check_private_key(context.get()) != 1 ||
      SSL_CTX_load_verify_locations(context.get(), client_ca.c_str(),
                                    nullptr) != 1 ||
      SSL_CTX_set_purpose(context.get(), X509_PURPOSE_SSL_CLIENT) != 1) {
    error = "TLS certificate configuration failed";
    return pw::Status::InvalidArgument();
  }
  SSL_CTX_set_verify_depth(context.get(), 4);
  SSL_CTX_set_session_cache_mode(context.get(), SSL_SESS_CACHE_OFF);
  SSL_CTX_set_options(context.get(),
                      SSL_OP_NO_RENEGOTIATION | SSL_OP_NO_TICKET);
  SSL_CTX_set_mode(context.get(), SSL_MODE_ENABLE_PARTIAL_WRITE |
                                      SSL_MODE_ACCEPT_MOVING_WRITE_BUFFER);
  SSL_CTX_set_verify(context.get(),
                     SSL_VERIFY_PEER | SSL_VERIFY_FAIL_IF_NO_PEER_CERT,
                     nullptr);
  return context;
}

pw::Result<int> CreateListener(const std::string& bind_address,
                               const std::string& port, std::string& error) {
  addrinfo hints{};
  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = SOCK_STREAM;
  hints.ai_flags = AI_PASSIVE;
  addrinfo* addresses = nullptr;
  const char* host = bind_address == "*" ? nullptr : bind_address.c_str();
  const int lookup = getaddrinfo(host, port.c_str(), &hints, &addresses);
  if (lookup != 0) {
    error = gai_strerror(lookup);
    return pw::Status::InvalidArgument();
  }
  int listener = -1;
  for (addrinfo* address = addresses; address != nullptr;
       address = address->ai_next) {
    listener =
        socket(address->ai_family, address->ai_socktype, address->ai_protocol);
    if (listener < 0) continue;
    int enabled = 1;
    setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &enabled, sizeof(enabled));
    if (bind(listener, address->ai_addr, address->ai_addrlen) == 0 &&
        listen(listener, 128) == 0)
      break;
    close(listener);
    listener = -1;
  }
  freeaddrinfo(addresses);
  if (listener < 0) {
    error = "unable to bind TCP listener";
    return pw::Status::Unavailable();
  }
  return listener;
}

struct GatewayStartup {
  GatewayAcl acl;
  TlsContext tls_context;
  int listener{-1};
};

pw::Result<GatewayStartup> InitializeGateway(
    const std::string& certificate, const std::string& private_key,
    const std::string& client_ca, const std::string& acl_path,
    const std::string& staging_dir, const std::string& bind_address,
    const std::string& port, std::string& error) {
  PW_TRY_ASSIGN(GatewayAcl acl, GatewayAcl::Load(acl_path, error));
  g_staging_root = staging_dir;
  PW_TRY(PrepareStagingRoot(g_staging_root, error));
  if (acl.size() == 0) {
    PW_LOG_WARN(
        "event=acl_empty detail=every client dispatch is denied; pass --acl "
        "FILE");
  }
  PW_TRY_ASSIGN(TlsContext tls_context,
                CreateTlsContext(certificate, private_key, client_ca, error));
  PW_TRY_ASSIGN(const int listener, CreateListener(bind_address, port, error));
  return GatewayStartup{std::move(acl), std::move(tls_context), listener};
}

std::string Argument(int argc, char** argv, std::string_view name,
                     std::string fallback = {}) {
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
    } else {
      ++it;
    }
  }
}
}  // namespace

int main(int argc, char** argv) {
  pw::log_basic::SetOutput(WriteGatewayLog);
  constexpr std::string_view kUsage =
      "usage: mcubridge-gateway-cpp --cert FILE --key FILE "
      "--client-ca FILE [--acl FILE] [--staging-dir DIR] [--bind ADDRESS] "
      "[--port PORT]\n";
  for (int i = 1; i < argc; ++i) {
    const std::string_view arg(argv[i]);
    if (arg == "--help" || arg == "-h") {
      std::cout << kUsage;
      return 0;
    }
  }
  const std::string certificate = Argument(argc, argv, "--cert");
  const std::string private_key = Argument(argc, argv, "--key");
  const std::string client_ca = Argument(argc, argv, "--client-ca");
  const std::string bind_address = Argument(argc, argv, "--bind", "*");
  const std::string port = Argument(argc, argv, "--port", "8443");
  const std::string acl_path = Argument(argc, argv, "--acl");
  const std::string staging_dir =
      Argument(argc, argv, "--staging-dir", "/var/lib/mcubridge-gateway");
  if (certificate.empty() || private_key.empty() || client_ca.empty()) {
    std::cerr << kUsage;
    return 2;
  }
  std::string error;
  auto startup =
      InitializeGateway(certificate, private_key, client_ca, acl_path,
                        staging_dir, bind_address, port, error);
  if (!startup.ok()) {
    PW_LOG_CRITICAL("fatal=%s", error.c_str());
    return 1;
  }
  signal(SIGINT, HandleSignal);
  signal(SIGTERM, HandleSignal);
  signal(SIGPIPE, SIG_IGN);
  DeviceSessionRegistry device_registry;
  ClientSessionRegistry client_registry;
  GatewayMetrics gateway_metrics;
  std::vector<std::unique_ptr<SessionTask>> tasks;
  const int listener = startup->listener;
  PW_LOG_INFO(
      "event=gateway_listening bind=%s port=%s tls=1.3 identity=uri_san",
      bind_address.c_str(), port.c_str());
  while (!g_stopping.load()) {
    ReapTasks(tasks);
    pollfd descriptor{listener, POLLIN, 0};
    const int ready = poll(&descriptor, 1, 500);
    if (ready < 0) {
      if (errno == EINTR) continue;
      PW_LOG_CRITICAL("fatal=listener poll failed");
      break;
    }
    if (ready == 0 || (descriptor.revents & POLLIN) == 0) continue;
    sockaddr_storage peer{};
    socklen_t peer_size = sizeof(peer);
    const int client_fd =
        accept(listener, reinterpret_cast<sockaddr*>(&peer), &peer_size);
    if (client_fd < 0) {
      if (errno == EINTR || errno == EAGAIN) continue;
      PW_LOG_WARN("event=accept_failed errno=%d", errno);
      continue;
    }
    if (tasks.size() >= kMaxSessions) {
      gateway_metrics.client_rejections_total.Increment();
      close(client_fd);
      continue;
    }
    auto task = std::make_unique<SessionTask>(
        client_fd, startup->tls_context.get(), device_registry, client_registry,
        startup->acl, gateway_metrics);
    SessionTask* task_ptr = task.get();
    task->worker = pw::thread::Thread(pw::thread::stl::Options(),
                                      [task_ptr] { task_ptr->Run(); });
    tasks.push_back(std::move(task));
  }
  close(listener);
  for (auto& task : tasks) {
    std::lock_guard lock(task->mutex);
    if (task->session != nullptr)
      task->session->Stop();
    else if (task->fd >= 0)
      shutdown(task->fd, SHUT_RDWR);
  }
  for (auto& task : tasks)
    if (task->worker.joinable()) task->worker.join();
  PW_LOG_INFO("event=gateway_stopped");
  return 0;
}
