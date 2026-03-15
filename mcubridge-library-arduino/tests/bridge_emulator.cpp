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
#include "services/Console.h"
#include "host_serial_stream.h"

// External delegate for stream
Stream* g_arduino_stream_delegate = nullptr;

HostSerialStream<true> MySerial;
HardwareSerial Serial;
HardwareSerial Serial1;

// --- Millis Implementation ---
static struct timespec g_start_time;
static bool g_timer_initialized = false;

unsigned long millis() {
  if (!g_timer_initialized) {
    clock_gettime(CLOCK_MONOTONIC, &g_start_time);
    g_timer_initialized = true;
  }
  struct timespec now;
  clock_gettime(CLOCK_MONOTONIC, &now);
  return (now.tv_sec - g_start_time.tv_sec) * 1000 +
         (now.tv_nsec - g_start_time.tv_nsec) / 1000000;
}

int main(int argc, char** argv) {
  const char* port = "/tmp/ttyBRIDGE0";
  if (argc > 1) port = argv[1];

  int fd = open(port, O_RDWR | O_NOCTTY);
  if (fd < 0) {
    fprintf(stderr, "Fallback to stdio for port %s\n", port);
    MySerial.setFds(STDIN_FILENO, STDOUT_FILENO);
  } else {
    struct termios tty;
    if (tcgetattr(fd, &tty) == 0) {
      cfsetospeed(&tty, B115200); cfsetispeed(&tty, B115200);
      tty.c_cflag = (tty.c_cflag & ~CSIZE) | CS8; tty.c_iflag &= ~IGNBRK;
      tty.c_lflag = 0; tty.c_oflag = 0; tty.c_cc[VMIN] = 1; tty.c_cc[VTIME] = 5;
      tty.c_cflag |= (CLOCAL | CREAD); tty.c_cflag &= ~(PARENB | PARODD);
      tty.c_cflag &= ~CSTOPB; tty.c_cflag &= ~CRTSCTS;
      tcsetattr(fd, TCSANOW, &tty);
    }
    MySerial.setFds(fd, fd);
  }

  g_arduino_stream_delegate = &MySerial;
  Bridge.begin(115200);

  fprintf(stderr, "McuBridge Emulator Started on %s\n", port);

  while (true) {
    Bridge.process();
    while (Console.available()) {
      int c = Console.read();
      if (c >= 0) Console.write(static_cast<uint8_t>(c));
    }
    usleep(1000);
  }
  return 0;
}
