#!/bin/bash
set -e
# Get the script directory and then the project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Get standard library path
ARDUINO_LIBS="$HOME/Arduino/libraries"
if [ ! -d "$ARDUINO_LIBS" ]; then
    ARDUINO_LIBS="$HOME/Documents/Arduino/libraries"
fi

# Define explicit include paths for official libraries
ETL_PATH="$ARDUINO_LIBS/Embedded_Template_Library"
WOLFSSL_PATH="$ARDUINO_LIBS/wolfSSL"
PACKETSERIAL_PATH="$ARDUINO_LIBS/PacketSerial"
MPACK_PATH="$ARDUINO_LIBS/mpack"

# Fallback to local .dummy_libs for CI or if standard paths are missing
if [ ! -d "$ETL_PATH" ]; then
    ETL_PATH="$REPO_ROOT/.dummy_libs/Embedded_Template_Library"
fi
if [ ! -d "$WOLFSSL_PATH" ]; then
    WOLFSSL_PATH="$REPO_ROOT/.dummy_libs/wolfSSL"
fi
if [ ! -d "$PACKETSERIAL_PATH" ]; then
    PACKETSERIAL_PATH="$REPO_ROOT/.dummy_libs/PacketSerial"
fi
if [ ! -d "$MPACK_PATH" ]; then
    MPACK_PATH="$REPO_ROOT/.dummy_libs/mpack"
fi

CFLAGS="-std=c++17 -O0 -g -DBRIDGE_HOST_TEST=1 -DUNITY_INCLUDE_DOUBLE -DBRIDGE_ENABLE_SPI=1  -DWOLFSSL_USER_SETTINGS -DETL_NO_STL -Isrc -Isrc/config -Isrc/protocol -Itests/Unity/src -I../tools/arduino_stub/include -I$ETL_PATH -I$ETL_PATH/include -I$ETL_PATH/arduino -I$WOLFSSL_PATH -I$PACKETSERIAL_PATH -I$PACKETSERIAL_PATH/src -I$MPACK_PATH/src"
SOURCES="src/security/security.cpp src/hal/hal.cpp src/protocol/rle.cpp src/Bridge.cpp src/fsm/bridge_fsm.cpp src/services/Console.cpp src/services/DataStore.cpp src/services/Mailbox.cpp src/services/FileSystem.cpp src/services/Process.cpp src/services/SPIService.cpp ../tools/arduino_stub/ArduinoStubs.cpp tests/test_host_filesystem_mock.cpp"
WOLF_SOURCES="$WOLFSSL_PATH/wolfcrypt/src/sha256.c $WOLFSSL_PATH/wolfcrypt/src/hmac.c $WOLFSSL_PATH/wolfcrypt/src/hash.c $WOLFSSL_PATH/wolfcrypt/src/kdf.c $WOLFSSL_PATH/wolfcrypt/src/error.c $WOLFSSL_PATH/wolfcrypt/src/logging.c $WOLFSSL_PATH/wolfcrypt/src/wc_port.c $WOLFSSL_PATH/wolfcrypt/src/memory.c"
MPACK_SOURCES="$MPACK_PATH/src/mpack-common.c $MPACK_PATH/src/mpack-writer.c $MPACK_PATH/src/mpack-reader.c $MPACK_PATH/src/mpack-expect.c $MPACK_PATH/src/mpack-node.c $MPACK_PATH/src/mpack-platform.c"
UNITY="build-host-local/unity.o"

TESTS="test_msgpack test_protocol test_bridge_core test_bridge_components test_fsm_mutual_auth test_integrated test_host_filesystem test_arduino_100_coverage test_coverage_full test_coverage_hardened"

# Unity object file
mkdir -p build-host-local
if [ ! -f $UNITY ]; then
  gcc -O0 -g -c tests/Unity/src/unity.c -o $UNITY
fi

for t in $TESTS; do
  echo "=== Building $t ==="
  g++ $CFLAGS $SOURCES $WOLF_SOURCES $MPACK_SOURCES tests/${t}.cpp $UNITY -o build-host-local/${t} 2>&1
  echo "=== Running $t ==="
  ./build-host-local/${t} 2>&1
  echo "=== $t DONE ==="
done
echo "ALL_TESTS_PASSED"
