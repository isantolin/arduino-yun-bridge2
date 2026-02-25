#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <poll.h>
#include <time.h>
#include <termios.h>
#include <fcntl.h>

#define BRIDGE_HOST_TEST 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_frame.h"

using namespace rpc;

class HostSerialStream : public Stream {
public:
    size_t write(uint8_t c) override { 
        size_t n = ::write(STDOUT_FILENO, &c, 1);
        tcdrain(STDOUT_FILENO);
        return n;
    }
    size_t write(const uint8_t* buffer, size_t size) override { 
        size_t n = ::write(STDOUT_FILENO, buffer, size);
        tcdrain(STDOUT_FILENO);
        return n;
    }
    int available() override {
        struct pollfd fds;
        fds.fd = STDIN_FILENO;
        fds.events = POLLIN;
        return (poll(&fds, 1, 0) > 0) ? 1 : 0;
    }
    int read() override {
        uint8_t c;
        if (::read(STDIN_FILENO, &c, 1) == 1) return c;
        return -1;
    }
    int peek() override { return -1; }
    void flush() override { tcdrain(STDOUT_FILENO); }
};

Stream* g_arduino_stream_delegate = nullptr;
HostSerialStream HostSerial;
HardwareSerial Serial;
HardwareSerial Serial1;

unsigned long millis() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (ts.tv_sec * 1000) + (ts.tv_nsec / 1000000);
}

void delay(unsigned long ms) { usleep(ms * 1000); }

int main() {
    // Force NON-BLOCKING and RAW mode for absolute binary transparency
    int flags = fcntl(STDIN_FILENO, F_GETFL, 0);
    fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK);

    struct termios t;
    if (tcgetattr(STDIN_FILENO, &t) == 0) {
        cfmakeraw(&t);
        tcsetattr(STDIN_FILENO, TCSANOW, &t);
    }
    if (tcgetattr(STDOUT_FILENO, &t) == 0) {
        cfmakeraw(&t);
        tcsetattr(STDOUT_FILENO, TCSANOW, &t);
    }

    setvbuf(stdin, NULL, _IONBF, 0);
    setvbuf(stdout, NULL, _IONBF, 0);
    g_arduino_stream_delegate = &HostSerial;
    srand(time(NULL));

    const char* secret = "DEBUG_INSECURE";
    Bridge.begin(115200, secret, 14);

    while (true) {
        Bridge.process();
        struct timespec ts = {0, 100000}; // 100us
        nanosleep(&ts, NULL);
    }
    return 0;
}
