#!/usr/bin/env python3
"""Generate upstream Pigweed Nanopb RPC bindings for mcu_wire.proto."""

import sys
import re

from google.protobuf.compiler import plugin_pb2
from pw_rpc import codegen_nanopb


def main() -> int:
    request = plugin_pb2.CodeGeneratorRequest.FromString(sys.stdin.buffer.read())
    response = plugin_pb2.CodeGeneratorResponse()
    targets = set(request.file_to_generate)

    if "mcu_wire.proto" in targets:
        proto_file = next(file for file in request.proto_file if file.name == "mcu_wire.proto")
        for output_file in codegen_nanopb.process_proto_file(proto_file):
            output = response.file.add()
            output.name = output_file.name()
            output.content = re.sub(
                r"^// on .*\n", "", output_file.content(), count=1, flags=re.MULTILINE
            )

    response.supported_features |= response.FEATURE_PROTO3_OPTIONAL
    sys.stdout.buffer.write(response.SerializeToString())
    return 0


if __name__ == "__main__":
    sys.exit(main())
