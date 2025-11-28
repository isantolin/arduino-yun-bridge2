import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from yunbridge.config import credentials


def test_load_credentials_file_accepts_secure_permissions(tmp_path):
    cred_file = tmp_path / "creds"
    cred_file.write_text("FOO=bar\n", encoding="utf-8")
    cred_file.chmod(0o600)

    data = credentials.load_credentials_file(cred_file)

    assert data == {"FOO": "bar"}


def test_load_credentials_file_rejects_world_readable(tmp_path):
    cred_file = tmp_path / "creds"
    cred_file.write_text("FOO=bar\n", encoding="utf-8")
    cred_file.chmod(0o644)

    with pytest.raises(PermissionError):
        credentials.load_credentials_file(cred_file)


def test_load_credentials_file_rejects_unexpected_owner(tmp_path, monkeypatch):
    cred_file = tmp_path / "creds"
    cred_file.write_text("FOO=bar\n", encoding="utf-8")
    cred_file.chmod(0o600)

    real_stat = Path.stat

    def _fake_stat(self: Path):  # type: ignore[override]
        result = real_stat(self)
        if self == cred_file:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_uid=9999,
            )
        return result

    monkeypatch.setattr(credentials.os, "geteuid", lambda: 0)
    monkeypatch.setattr(Path, "stat", _fake_stat)

    with pytest.raises(PermissionError):
        credentials.load_credentials_file(cred_file)


def test_lookup_credential_prefers_blank_env_over_file():
    result = credentials.lookup_credential(
        ("YUNBRIDGE_MQTT_USER",),
        credential_map={"YUNBRIDGE_MQTT_USER": "fromfile"},
        environ={"YUNBRIDGE_MQTT_USER": "   "},
        fallback="fallback",
    )

    assert result == ""


def test_lookup_credential_allows_blank_file_value():
    result = credentials.lookup_credential(
        ("YUNBRIDGE_MQTT_USER",),
        credential_map={"YUNBRIDGE_MQTT_USER": "   "},
        environ={},
        fallback="fallback",
    )

    assert result == ""
