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
#include "host_serial_stream.h"

// External delegate for stream
Stream* g_arduino_stream_delegate = nullptr;

HostSerialStream<true> MySerial;
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
  int fd_in = STDIN_FILENO;
  int fd_out = STDOUT_FILENO;
  if (pty_path) {
    int pty_fd = open(pty_path, O_RDWR | O_NOCTTY);
    if (pty_fd >= 0) {
      fd_in = pty_fd;
      fd_out = pty_fd;
      
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
  MySerial.setFds(fd_in, fd_out);

  setvbuf(stdout, NULL, _IONBF, 0);
  setvbuf(stdin, NULL, _IONBF, 0);
  
  int flags = fcntl(fd_in, F_GETFL, 0);
  fcntl(fd_in, F_SETFL, flags | O_NONBLOCK);
  
  g_arduino_stream_delegate = &MySerial;
  srand(time(NULL));
  const char* secret = "DEBUG_INSECURE";
  Bridge.begin(115200, secret, 14);
  while (true) {
    Bridge.process();
    
    // Console Echo Implementation
    while (Console.available()) {
      int c = Console.read();
      if (c >= 0) {
        Console.write(static_cast<uint8_t>(c));
      }
    }

    usleep(1000);
  }
  return 0;
}
