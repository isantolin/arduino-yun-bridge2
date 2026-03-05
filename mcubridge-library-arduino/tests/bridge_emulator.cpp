#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#define BRIDGE_HOST_TEST 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"

// External delegate for stream
Stream* g_arduino_stream_delegate = nullptr;

static int g_fd_in = STDIN_FILENO;
static int g_fd_out = STDOUT_FILENO;

class HostSerialStream : public Stream {
 public:
  size_t write(uint8_t c) override {
    fprintf(stderr, "[MCU -> SERIAL] %02X\n", c);
    size_t n = ::write(g_fd_out, &c, 1);
    fsync(g_fd_out);
    return n;
  }
  
  size_t write(const uint8_t* buffer, size_t size) override {
    for (size_t i = 0; i < size; i++) {
      fprintf(stderr, "[MCU -> SERIAL] %02X\n", buffer[i]);
    }
    size_t n = ::write(g_fd_out, buffer, size);
    fsync(g_fd_out);
    return n;
  }
  
  int available() override {
    struct pollfd fds;
    fds.fd = g_fd_in;
    fds.events = POLLIN;
    if (poll(&fds, 1, 0) > 0) {
      if (fds.revents & POLLIN) return 1;
    }
    return 0;
  }

  int read() override {
    uint8_t c;
    ssize_t res = ::read(g_fd_in, &c, 1);
    if (res == 1) {
      fprintf(stderr, "[SERIAL -> MCU] %02X\n", c);
      return c;
    }
    return -1;
  }

  
  int peek() override { return -1; }
  void flush() override { fsync(g_fd_out); }
};

HostSerialStream MySerial;
HardwareSerial Serial;
HardwareSerial Serial1;

// --- Millis Implementation ---
unsigned long millis() {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (unsigned long)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
}

void delay(uint32_t ms) {
  struct timespec ts;
  ts.tv_sec = ms / 1000;
  ts.tv_nsec = (ms % 1000) * 1000000;
  nanosleep(&ts, NULL);
}

int main(int argc, char** argv) {
  (void)argc; (void)argv;
  
  const char* pty_path = getenv("MCU_PTY");
  if (pty_path) {
    int pty_fd = open(pty_path, O_RDWR | O_NOCTTY);
    if (pty_fd >= 0) {
      g_fd_in = pty_fd;
      g_fd_out = pty_fd;
      
      // ENSURE RAW MODE ON THE PTY TO PREVENT CORRUPTION
      struct termios t;
      if (tcgetattr(pty_fd, &t) == 0) {
        cfmakeraw(&t);
        t.c_cflag &= ~CSIZE;
        t.c_cflag |= CS8;
        t.c_cflag &= ~PARENB;
        tcsetattr(pty_fd, TCSANOW, &t);
      }
      
      fprintf(stderr, "Using dedicated PTY: %s\n", pty_path);
    }
  }

  setvbuf(stdout, NULL, _IONBF, 0);
  setvbuf(stdin, NULL, _IONBF, 0);
  
  int flags = fcntl(g_fd_in, F_GETFL, 0);
  fcntl(g_fd_in, F_SETFL, flags | O_NONBLOCK);
  
  g_arduino_stream_delegate = &MySerial;
  srand(time(NULL));
  const char* secret = "DEBUG_INSECURE";
  Bridge.begin(115200, secret, 14);
  while (true) {
    Bridge.process();
    usleep(1000);
  }
  return 0;
}
