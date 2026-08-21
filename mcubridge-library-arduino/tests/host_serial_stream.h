#pragma once

#include <Arduino.h>
#include <etl/algorithm.h>
#include <etl/span.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/ioctl.h>
#include <unistd.h>

/**
 * @brief Real-time Stream implementation for Linux Host.
 *
 * Maps Arduino Serial calls to Linux file descriptors.
 * @tparam Debug  When true, log every byte to stderr.
 */
template <bool Debug = false>
class HostSerialStream : public Stream {
 public:
  HostSerialStream() : fd_in_(STDIN_FILENO), fd_out_(STDOUT_FILENO) {}
  HostSerialStream(int fd_in, int fd_out) : fd_in_(fd_in), fd_out_(fd_out) {}

  void setFds(int fd_in, int fd_out) {
    fd_in_ = fd_in;
    fd_out_ = fd_out;
  }

  size_t write(uint8_t c) override {
    if (Debug) fprintf(stderr, "[MCU -> SERIAL] %02X\n", c);
    size_t n = ::write(fd_out_, &c, 1);
    fsync(fd_out_);
    return n;
  }

  size_t write(const uint8_t* buffer, size_t size) override {
    if (Debug) {
      etl::span<const uint8_t> buf_span(buffer, size);
      etl::for_each(buf_span.begin(), buf_span.end(), [](uint8_t b) {
        fprintf(stderr, "[MCU -> SERIAL] %02X\n", b);
      });
    }
    size_t n = ::write(fd_out_, buffer, size);
    fsync(fd_out_);
    return n;
  }

  int available() override {
    int count = 0;
    if (ioctl(fd_in_, FIONREAD, &count) < 0) {
      return 0;
    }
    return count;
  }

  int read() override {
    uint8_t c;
    ssize_t res = ::read(fd_in_, &c, 1);
    if (res == 1) {
      if (Debug) fprintf(stderr, "[SERIAL -> MCU] %02X\n", c);
      return c;
    }
    return -1;
  }

  int peek() override { return -1; }
  void flush() override { fsync(fd_out_); }

 private:
  int fd_in_;
  int fd_out_;
};
