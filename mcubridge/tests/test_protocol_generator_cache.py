from pathlib import Path

from tools.protocol import generate


def test_incremental_build_invalidates_changed_inputs_and_missing_outputs(tmp_path: Path) -> None:
    proto_dir = tmp_path / "tools" / "protocol"
    template_dir = proto_dir / "templates"
    template_dir.mkdir(parents=True)
    input_files = {
        proto_dir / "mcubridge.proto": 'syntax = "proto3";\n',
        proto_dir / "buf.yaml": "version: v2\n",
        proto_dir / "buf.gen.yaml": "version: v2\n",
        proto_dir / "mcubridge.options": "mcubridge.Message max_size:64\n",
        tmp_path / "requirements" / "runtime.toml": '[[dependency]]\nname = "generator"\n',
        tmp_path / ".github" / "actions" / "setup-protoc" / "action.yml": "name: setup-protoc\n",
        template_dir / "protocol.py.j2": "{{ protocol_version }}\n",
        tmp_path / "VERSION": "2.1.0\n",
    }
    for path, content in input_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    args = generate.GenerationArgs(
        spec=proto_dir / "mcubridge.proto",
        cpp=tmp_path / "out" / "cpp" / "rpc_protocol.h",
        cpp_structs=tmp_path / "out" / "cpp" / "rpc_structs.h",
        py=tmp_path / "out" / "python" / "protocol.py",
        py_client=tmp_path / "out" / "client" / "protocol_client.py",
    )
    up_to_date, hash_file, current_hash = generate.check_incremental_build(
        args,
        "2.1.0",
        repo_root=tmp_path,
    )
    assert not up_to_date

    outputs = generate.expected_generation_outputs(args, repo_root=tmp_path)
    assert outputs
    for output in outputs:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("generated\n", encoding="utf-8")
    hash_file.write_text(current_hash, encoding="utf-8")

    cached, _, _ = generate.check_incremental_build(args, "2.1.0", repo_root=tmp_path)
    assert cached

    for changed_input in (
        proto_dir / "mcubridge.options",
        proto_dir / "buf.gen.yaml",
        tmp_path / "requirements" / "runtime.toml",
        template_dir / "protocol.py.j2",
    ):
        original_content = changed_input.read_text(encoding="utf-8")
        changed_input.write_text(original_content + "# changed\n", encoding="utf-8")
        invalidated, _, changed_hash = generate.check_incremental_build(
            args,
            "2.1.0",
            repo_root=tmp_path,
        )
        assert not invalidated
        assert changed_hash != current_hash
        changed_input.write_text(original_content, encoding="utf-8")
        restored, _, restored_hash = generate.check_incremental_build(
            args,
            "2.1.0",
            repo_root=tmp_path,
        )
        assert restored
        assert restored_hash == current_hash

    hash_file.write_text(current_hash, encoding="utf-8")
    outputs[0].unlink()
    missing_output, _, _ = generate.check_incremental_build(args, "2.1.0", repo_root=tmp_path)
    assert not missing_output
