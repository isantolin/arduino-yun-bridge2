#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <poll.h>
#include <time.h>

#define BRIDGE_HOST_TEST 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_frame.h"

using namespace rpc;

/**
 * @brief Real-time Stream implementation for Linux Host.
 * Maps Arduino Serial calls to Linux stdin/stdout.
 */
class HostSerialStream : public Stream {
public:
    size_t write(uint8_t c) override {
        return ::write(STDOUT_FILENO, &c, 1);
    }
    
    size_t write(const uint8_t* buffer, size_t size) override {
        return ::write(STDOUT_FILENO, buffer, size);
    }
    
    int available() override {
        struct pollfd fds;
        fds.fd = STDIN_FILENO;
        fds.events = POLLIN;
        int ret = poll(&fds, 1, 0);
        return (ret > 0 && (fds.revents & POLLIN)) ? 1 : 0;
    }
    
    int read() override {
        uint8_t c;
        if (::read(STDIN_FILENO, &c, 1) == 1) return c;
        return -1;
    }
    
    int peek() override { return -1; }
    void flush() override { fsync(STDOUT_FILENO); }
};

// Global instances for the Bridge
Stream* g_arduino_stream_delegate = nullptr;
HostSerialStream HostSerial;
HardwareSerial Serial;
HardwareSerial Serial1;

// Stubs for required Arduino symbols
unsigned long millis() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (ts.tv_sec * 1000) + (ts.tv_nsec / 1000000);
}

void delay(unsigned long ms) {
    usleep(ms * 1000);
}

// --- INCLUDE THE ACTUAL SKETCH CODE ---
// We need to define setup() and loop() from the .ino file.
// Since .ino files are just C++ without some headers, we can include it directly
// if we provide the necessary context.
#define main arduino_main // Rename sketch main if any
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
