#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define BRIDGE_HOST_TEST 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1

// Match BridgeControl.ino service configuration (must precede Bridge.h).
#define BRIDGE_ENABLE_DATASTORE 0
#define BRIDGE_ENABLE_FILESYSTEM 1
#define BRIDGE_ENABLE_PROCESS 0

#include "Bridge.h"
#include "host_serial_stream.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"

using namespace rpc;

// Global instances for the Bridge
Stream* g_arduino_stream_delegate = nullptr;
HostSerialStream<false> HostSerial;
HardwareSerial Serial;
HardwareSerial Serial1;

// Stubs for required Arduino symbols
unsigned long millis() {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (ts.tv_sec * 1000) + (ts.tv_nsec / 1000000);
}

void delay(unsigned long ms) { usleep(ms * 1000); }

// --- INCLUDE THE ACTUAL SKETCH CODE ---
// We need to define setup() and loop() from the .ino file.
// Since .ino files are just C++ without some headers, we can include it
// directly if we provide the necessary context.
#define main arduino_main  // Rename sketch main if any
#include "../examples/BridgeControl/BridgeControl.ino"
#undef main

int main() {
  // Disable buffering for stdin/stdout to ensure real-time serial behavior
  setvbuf(stdin, NULL, _IONBF, 0);
  setvbuf(stdout, NULL, _IONBF, 0);

  // Setup the delegate
  g_arduino_stream_delegate = &HostSerial;

  // Seed the random number generator
  srand(time(NULL));

  // Execute Arduino Lifecycle
  setup();

  while (true) {
    loop();
    // Small sleep to prevent 100% CPU usage while maintaining low latency
    usleep(100);
  }

  return 0;
}
