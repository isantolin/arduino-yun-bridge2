#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>
#include <signal.h>

#define BRIDGE_HOST_TEST 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"
#include "services/Console.h"
#include "host_serial_stream.h"

// External delegate for stream
Stream* g_arduino_stream_delegate = nullptr;

static volatile sig_atomic_t g_running = 1;

void signal_handler(int signum) {
  (void)signum;
  g_running = 0;
}

HostSerialStream<true> MySerial;
HardwareSerial Serial;
HardwareSerial Serial1;

// --- Millis Implementation ---
unsigned long millis() {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (ts.tv_sec * 1000) + (ts.tv_nsec / 1000000);
}

void delay(unsigned long ms) { usleep(ms * 1000); }

int main(int argc, char** argv) {
  (void)argc;
  (void)argv;
  // Disable buffering for stdin/stdout to ensure real-time serial behavior
  setvbuf(stdin, NULL, _IONBF, 0);
  setvbuf(stdout, NULL, _IONBF, 0);

  g_arduino_stream_delegate = &MySerial;
  fprintf(stderr, "[emulator] Simulated SD card at: /tmp/mcubridge-host-fs\n");

  struct sigaction sa;
  sa.sa_handler = signal_handler;
  sigemptyset(&sa.sa_mask);
  sa.sa_flags = 0;
  sigaction(SIGTERM, &sa, NULL);
  sigaction(SIGINT, &sa, NULL);

  Bridge.begin(rpc::DEFAULT_BAUDRATE, "DEBUG_INSECURE");

  fprintf(stderr, "McuBridge Emulator Started on stdio\n");

  while (g_running) {
    Bridge.process();
    while (Console.available()) {
      int c = Console.read();
      if (c >= 0) Console.write(static_cast<uint8_t>(c));
    }
    Console.process();
    usleep(1000);
  }
  fprintf(stderr, "McuBridge Emulator Terminating...\n");
  return 0;
}