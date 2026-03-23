#!/bin/bash
set -e
cd /home/ignaciosantolin/arduino-yun-bridge2/mcubridge-library-arduino

CFLAGS="-std=c++17 -O0 -g -DBRIDGE_HOST_TEST=1 -DBRIDGE_TEST_NO_GLOBALS=1 -DUNITY_INCLUDE_DOUBLE -DBRIDGE_ENABLE_SPI=1 -DWOLFSSL_USER_SETTINGS -Isrc -Isrc/config -Isrc/nanopb -Isrc/protocol -Itests/Unity -I../tools/arduino_stub/include -I/home/ignaciosantolin/Arduino/libraries/Embedded_Template_Library_ETL/src -I/home/ignaciosantolin/Arduino/libraries/wolfssl/src"
SOURCES="src/nanopb/pb_common.c src/nanopb/pb_encode.c src/nanopb/pb_decode.c src/protocol/mcubridge.pb.c src/security/security.cpp src/hal/hal.cpp src/protocol/rle.cpp src/protocol/rpc_cobs.cpp src/Bridge.cpp src/services/Console.cpp src/services/DataStore.cpp src/services/Mailbox.cpp src/services/FileSystem.cpp src/services/Process.cpp src/services/SPIService.cpp"
WOLF_SOURCES="/home/ignaciosantolin/Arduino/libraries/wolfssl/src/wolfcrypt/src/sha256.c /home/ignaciosantolin/Arduino/libraries/wolfssl/src/wolfcrypt/src/hmac.c /home/ignaciosantolin/Arduino/libraries/wolfssl/src/wolfcrypt/src/hash.c /home/ignaciosantolin/Arduino/libraries/wolfssl/src/wolfcrypt/src/kdf.c /home/ignaciosantolin/Arduino/libraries/wolfssl/src/wolfcrypt/src/error.c /home/ignaciosantolin/Arduino/libraries/wolfssl/src/wolfcrypt/src/logging.c /home/ignaciosantolin/Arduino/libraries/wolfssl/src/wolfcrypt/src/wc_port.c /home/ignaciosantolin/Arduino/libraries/wolfssl/src/wolfcrypt/src/memory.c"
UNITY="build-host-local/unity.o"

TESTS="test_protocol test_bridge_core test_bridge_components test_fsm_mutual_auth test_integrated"

# Unity object file
mkdir -p build-host-local
if [ ! -f $UNITY ]; then
  gcc -O0 -g -c tests/Unity/unity.c -o $UNITY
fi

for t in $TESTS; do
  echo "=== Building $t ==="
  g++ $CFLAGS $SOURCES $WOLF_SOURCES tests/${t}.cpp $UNITY -o build-host-local/${t} 2>&1
  echo "=== Running $t ==="
  ./build-host-local/${t} 2>&1
  echo "=== $t DONE ==="
done
echo "ALL_TESTS_PASSED"
