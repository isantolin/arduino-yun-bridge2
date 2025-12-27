import os
import subprocess
import sys

def check_cpp_syntax():
    # Base directories
    base_dir = os.path.abspath("openwrt-library-arduino/src")
    protocol_dir = os.path.join(base_dir, "protocol")
    
    # Mock Arduino directory (using existing stub)
    stub_dir = os.path.abspath("tools/arduino_stub/include")
    
    # Files to check (only checking protocol files for now as they are the most critical and standalone)
    # We can't easily check Bridge.cpp because it depends heavily on Arduino hardware libraries (Stream, Serial, etc)
    # which are hard to mock fully without a lot of work. 
    # However, checking rpc_frame.cpp is critical because of the missing constants issue we fixed.
    files_to_check = [
        os.path.join(protocol_dir, "rpc_frame.cpp"),
        os.path.join(protocol_dir, "crc.cpp")
    ]
    
    include_paths = [
        f"-I{base_dir}",
        f"-I{stub_dir}",
        f"-I{protocol_dir}"
    ]
    
    flags = ["-fsyntax-only", "-std=c++11", "-Wall", "-Werror"]
    
    success = True
    for file_path in files_to_check:
        cmd = ["g++"] + flags + include_paths + [file_path]
        print(f"Checking syntax for {os.path.basename(file_path)}...")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"FAILED: {file_path}")
                print(result.stderr)
                success = False
            else:
                print(f"OK: {file_path}")
        except FileNotFoundError:
            print("Error: g++ not found. Cannot check C++ syntax.")
            return False

    return success

if __name__ == "__main__":
    if check_cpp_syntax():
        sys.exit(0)
    else:
        sys.exit(1)
