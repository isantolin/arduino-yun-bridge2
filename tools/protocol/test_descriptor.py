from pathlib import Path
import subprocess
from google.protobuf import descriptor_pb2

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROTO_DIR = REPO_ROOT / "tools/protocol"
NANOPB_PROTO_DIR = Path("/home/ignaciosantolin/.local/lib/python3.14/site-packages/nanopb/generator/proto")

desc_file = REPO_ROOT / "proto.desc"

cmd = [
    "protoc",
    f"--proto_path={PROTO_DIR}",
    f"--proto_path={NANOPB_PROTO_DIR}",
    f"--descriptor_set_out={desc_file}",
    "--include_imports",
    str(PROTO_DIR / "mcubridge.proto"),
]
subprocess.run(cmd, check=True)

with open(desc_file, "rb") as f:
    fds = descriptor_pb2.FileDescriptorSet()
    fds.ParseFromString(f.read())

# Build a pool to handle imports/extensions
from google.protobuf import descriptor_pool

pool = descriptor_pool.DescriptorPool()
for fd in fds.file:
    pool.Add(fd)

# Now we can access mcubridge.proto
mcubridge_fd = pool.FindFileByName("mcubridge.proto")
command_id_enum = mcubridge_fd.enum_types_by_name["CommandId"]

# Access extensions
bridge_options_fd = pool.FindFileByName("bridge_options.proto")

for value in command_id_enum.values:
    print(f"Command: {value.name} = {value.number}")
    # To access extensions from a DescriptorPool is a bit more involved
    # but we can do it!
