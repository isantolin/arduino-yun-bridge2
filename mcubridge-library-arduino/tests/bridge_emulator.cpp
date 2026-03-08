#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <stdarg.h>

#define BRIDGE_HOST_TEST 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"

// External delegate for stream
Stream* g_arduino_stream_delegate = nullptr;

static int g_fd_in = STDIN_FILENO;
static int g_fd_out = STDOUT_FILENO;

// Explicit file logger for MCU debug
void mcu_log(const char* fmt, ...) {
    FILE* f = fopen("/tmp/mcu_emulator.log", "a");
    if (!f) return;
    va_list args;
    va_start(args, fmt);
    vfprintf(f, fmt, args);
    va_end(args);
    fflush(f);
    fclose(f);
}

class HostSerialStream : public Stream {
 public:
  size_t write(uint8_t c) override {
    return ::write(g_fd_out, &c, 1);
  }
  
  size_t write(const uint8_t* buffer, size_t size) override {
    return ::write(g_fd_out, buffer, size);
  }
  
  int available() override {
    struct pollfd fds;
    fds.fd = g_fd_in;
    fds.events = POLLIN;
    // [FIX] FIONREAD is unreliable on PTYs. Use poll instead.
    int res = poll(&fds, 1, 0);
    if (res > 0 && (fds.revents & POLLIN)) {
      return 1;
    }
    return 0;
  }

  int read() override {
    uint8_t c;
    ssize_t res = ::read(g_fd_in, &c, 1);
    if (res == 1) {
      return static_cast<int>(c);
    }
    // Bridge.cpp now correctly handles b < 0, so EAGAIN is safe.
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
  
  FILE* clear_log = fopen("/tmp/mcu_emulator.log", "w");
  if (clear_log) fclose(clear_log);

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
    }
  }

  setvbuf(stdout, NULL, _IONBF, 0);
  setvbuf(stdin, NULL, _IONBF, 0);
  
  int flags = fcntl(g_fd_in, F_GETFL, 0);
  fcntl(g_fd_in, F_SETFL, flags | O_NONBLOCK);
  
  g_arduino_stream_delegate = &MySerial;
  srand(time(NULL));
  const char* secret = "DEBUG_INSECURE_16";
  Bridge.begin(115200, secret, 17);
  while (true) {
    Bridge.process();
    
    // Console Echo Implementation
    while (Console.available() > 0) {
      int c = Console.read();
      if (c >= 0) {
        Console.write(static_cast<uint8_t>(c));
      }
    }

    usleep(1000);
  }
  
  return 0;
}
