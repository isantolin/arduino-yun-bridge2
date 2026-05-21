from pathlib import Path

path = Path("tools/ci_arduino_host_tests.sh")
content = path.read_text()

test_files = """    "${TEST_DIR}/test_integrated.cpp"
    "${TEST_DIR}/test_bridge_core.cpp"
    "${TEST_DIR}/test_bridge_components.cpp"
    "${TEST_DIR}/test_host_filesystem.cpp"
    "${TEST_DIR}/test_protocol.cpp"
    "${TEST_DIR}/test_fsm_mutual_auth.cpp"
    "${TEST_DIR}/test_arduino_100_coverage.cpp"
    "${TEST_DIR}/test_coverage_full.cpp"
    "${TEST_DIR}/test_rpc_structs.cpp"
    "${TEST_DIR}/test_surgical_coverage.cpp"
    "${TEST_DIR}/test_arduino_harden.cpp"
    "${TEST_DIR}/test_arduino_crypto_harden.cpp"
    "${TEST_DIR}/test_arduino_stress.cpp"
    "${TEST_DIR}/test_rle.cpp"
    "${TEST_DIR}/test_coverage_hardened.cpp"
    "${TEST_DIR}/test_bridge_edge_paths.cpp"
    "${TEST_DIR}/test_hal_weak_defaults.cpp\""""

content = re.sub(r"TEST_FILES=\(.*?\)", f"TEST_FILES=(\\n{test_files}\\n)", content, flags=re.DOTALL)

path.write_text(content)
