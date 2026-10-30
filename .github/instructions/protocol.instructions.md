---
applyTo: "tools/protocol/**/*.proto,**/*_pb2.py,**/*.pb.h,**/*.pb.c,**/rpc_*.h"
---

Canonical Protobuf schemas are tools/protocol/config_schema.proto, tools/protocol/mcu_wire.proto, and tools/protocol/cloud_gateway.proto. Inspect imports and the generator before modifying schemas or generated output. Trace each affected contract across firmware, daemon, gateway, clients, examples, and tests. Regenerate outputs using the repository's generator; do not edit generated code as the source of truth. Evaluate compatibility and partial-upgrade behavior explicitly. Use generated Protobuf/Nanopb APIs directly where appropriate.