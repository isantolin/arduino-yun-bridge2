#!/bin/bash
set -e
cd /home/ignaciosantolin/arduino-yun-bridge2/mcubridge-library-arduino

CFLAGS="-std=c++14 -O0 -g -DBRIDGE_HOST_TEST=1 -DBRIDGE_TEST_NO_GLOBALS=1 -DUNITY_INCLUDE_DOUBLE -Isrc -Isrc/nanopb -Isrc/protocol -Itests/Unity -I../tools/arduino_stub/include"
SOURCES="src/nanopb/pb_common.c src/nanopb/pb_encode.c src/nanopb/pb_decode.c src/protocol/mcubridge.pb.c src/security/sha256.cpp src/security/security.cpp src/hal/hal.cpp src/protocol/rle.cpp src/protocol/rpc_cobs.cpp src/Bridge.cpp src/services/Console.cpp src/services/DataStore.cpp src/services/Mailbox.cpp src/services/FileSystem.cpp src/services/Process.cpp"
UNITY="build-host-local/unity.o"

TESTS="test_coverage_final_push test_coverage_mega"

for t in $TESTS; do
  echo "=== Building $t ==="
  g++ $CFLAGS $SOURCES tests/${t}.cpp $UNITY -o build-host-local/${t} 2>&1
  echo "=== Running $t ==="
  ./build-host-local/${t} 2>&1
  echo "=== $t DONE ==="
done
echo "ALL_TESTS_PASSED"
