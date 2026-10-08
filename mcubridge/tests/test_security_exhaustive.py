"""
This file is part of Arduino MCU Ecosystem v2.
Copyright (C) 2025-2026 Ignacio Santolin and contributors
"""

import struct
import pytest

from mcubridge.security.security import (
    NONCE_COUNTER_MASK,
    RPC_AEAD_NONCE_SIZE,
    RPC_AEAD_TAG_SIZE,
    SecurityError,
    SecurityManager,
    aead_decrypt_frame,
    aead_encrypt_frame,
)


def test_security_nonce_validation_via_public_api():
    """
    Verifica la decodificación y validación de nonces consumiendo la API pública,
    sin utilizar mocker.patch() sobre extract_nonce_counter().
    """
    key = b"\x0f" * 32
    cmd_id = 0x0001
    seq_id = 0x0001
    payload = b"AUTHENTICATED_DATA_PAYLOAD"

    # 1. Probar la generación y descifrado correcto de la trama usando la API pública
    counter_val = 42
    ciphertext, nonce, tag = aead_encrypt_frame(
        cmd_id=cmd_id,
        seq_id=seq_id,
        payload=payload,
        key=key,
        counter=counter_val,
    )

    # 2. Descifrar con la API pública real (ejecuta extract_nonce_counter internamente)
    decrypted_payload = aead_decrypt_frame(
        cmd_id=cmd_id,
        seq_id=seq_id,
        ciphertext=ciphertext,
        key=key,
        nonce=nonce,
        tag=tag,
    )

    assert decrypted_payload == payload

    # 3. Probar rechazo ante un nonce con formato/prefijo corrupto
    corrupted_nonce = b"BAD" + nonce[3:]  # Cambiar prefijo "MCU"/"MPU"
    with pytest.raises((ValueError, SecurityError)):
        aead_decrypt_frame(
            cmd_id=cmd_id,
            seq_id=seq_id,
            ciphertext=ciphertext,
            key=key,
            nonce=corrupted_nonce,
            tag=tag,
        )


def test_security_nonce_overflow_boundary_public_api():
    """
    Verifica los límites máximos del contador de nonce (2^64 - 1) y
    el rechazo de valores desbordados mediante la API pública de cifrado.
    """
    key = b"\x1a" * 32
    cmd_id = 0x0002
    seq_id = 0x0001
    payload = b"BOUNDARY_TEST"

    # 1. El límite máximo (NONCE_COUNTER_MASK = 2^64 - 1) debe ser aceptado
    _, nonce, _ = aead_encrypt_frame(
        cmd_id=cmd_id,
        seq_id=seq_id,
        payload=payload,
        key=key,
        counter=NONCE_COUNTER_MASK,
    )
    assert len(nonce) == RPC_AEAD_NONCE_SIZE

    # Extraer el contador empaquetado del nonce (bytes 4..12) para verificar big-endian real
    extracted_counter = struct.unpack(">Q", nonce[4:12])[0]
    assert extracted_counter == NONCE_COUNTER_MASK

    # 2. Superar el límite máximo debe lanzar ValueError ("Nonce counter overflow")
    with pytest.raises(ValueError, match="Nonce counter overflow"):
        aead_encrypt_frame(
            cmd_id=cmd_id,
            seq_id=seq_id,
            payload=payload,
            key=key,
            counter=NONCE_COUNTER_MASK + 1,
        )


def test_security_manager_replay_attack_prevention():
    """
    Verifica que el SecurityManager rastree e impida la reutilización de nonces
    viejos o repetidos a través del flujo normal de decodificación.
    """
    secret = b"SECRET_KEY_EXHAUSTIVE_TEST_32B"
    sec_mgr = SecurityManager(secret=secret)
    session_key = b"\x2b" * 32
    sec_mgr.set_session_key(session_key)

    cmd_id = 0x0003
    seq_id = 0x0001
    payload = b"REPLAY_PREVENTION_TEST"

    # Generar dos tramas consecutivas con contadores crecientes (100 y 101)
    ct1, nonce1, tag1 = aead_encrypt_frame(cmd_id, seq_id, payload, session_key, counter=100)
    ct2, nonce2, tag2 = aead_encrypt_frame(cmd_id, seq_id, payload, session_key, counter=101)

    # 1. Procesar trama 100 exitosamente
    res1 = sec_mgr.decrypt_incoming_frame(cmd_id, seq_id, ct1, nonce1, tag1)
    assert res1 == payload

    # 2. Procesar trama 101 exitosamente
    res2 = sec_mgr.decrypt_incoming_frame(cmd_id, seq_id, ct2, nonce2, tag2)
    assert res2 == payload

    # 3. Intentar procesar nuevamente la trama 100 (Replay Attack) debe ser rechazado
    with pytest.raises(SecurityError):
        sec_mgr.decrypt_incoming_frame(cmd_id, seq_id, ct1, nonce1, tag1)
