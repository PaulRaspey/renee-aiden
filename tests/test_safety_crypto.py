"""Tests for src.safety.memory_crypto."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.safety.config import MemoryEncryptionConfig
from src.safety.memory_crypto import (
    KEY_BYTES,
    MAGIC,
    MemoryVault,
    CryptoUnavailable,
    decrypt,
    derive_key,
    encrypt,
)


def test_encrypt_includes_magic_and_nonce():
    key = os.urandom(KEY_BYTES)
    blob = encrypt(b"hello", key)
    assert blob.startswith(MAGIC)
    assert len(blob) > len(MAGIC) + 12


def test_encrypt_decrypt_round_trip():
    key = os.urandom(KEY_BYTES)
    payload = b"Renee's memory: Paul said he's tired."
    blob = encrypt(payload, key)
    assert decrypt(blob, key) == payload


def test_decrypt_rejects_wrong_key():
    key = os.urandom(KEY_BYTES)
    other = os.urandom(KEY_BYTES)
    blob = encrypt(b"hi", key)
    with pytest.raises(Exception):
        decrypt(blob, other)


def test_decrypt_rejects_missing_magic():
    key = os.urandom(KEY_BYTES)
    with pytest.raises(ValueError):
        decrypt(b"nope nope nope nope nope nope", key)


def test_encrypt_rejects_wrong_key_length():
    with pytest.raises(ValueError):
        encrypt(b"hi", b"\x00" * 16)
    with pytest.raises(ValueError):
        decrypt(MAGIC + b"\x00" * 32, b"\x00" * 16)


def test_derive_key_creates_fallback_file(tmp_path: Path):
    cfg = MemoryEncryptionConfig(
        enabled=True,
        keyring_service="renee-test-service",
        keyring_username="no-such-user",
        fallback_key_filename=".test_memory_key",
    )
    key = derive_key(cfg, tmp_path)
    assert len(key) == KEY_BYTES
    fallback = tmp_path / ".test_memory_key"
    assert fallback.exists()
    # Second call returns the same key (the file is reused).
    key2 = derive_key(cfg, tmp_path)
    assert key2 == key


def test_derive_key_regenerates_when_file_wrong_length(tmp_path: Path):
    cfg = MemoryEncryptionConfig(
        enabled=True,
        fallback_key_filename=".broken_key",
    )
    broken = tmp_path / ".broken_key"
    broken.write_bytes(b"short")
    key = derive_key(cfg, tmp_path)
    assert len(key) == KEY_BYTES
    assert broken.read_bytes() == key


def test_derive_key_stashes_fresh_fallback_key_into_keyring(tmp_path: Path, monkeypatch):
    """Decision 54: keyring miss → fallback file created → stash into keyring."""
    from src.safety import memory_crypto as mc

    stashed: dict = {}
    monkeypatch.setattr(mc, "_try_keyring_get", lambda service, username: None)
    def fake_set(service, username, key):
        stashed["service"] = service
        stashed["username"] = username
        stashed["key"] = key
        return True
    monkeypatch.setattr(mc, "_try_keyring_set", fake_set)

    cfg = MemoryEncryptionConfig(
        enabled=True,
        keyring_service="svc",
        keyring_username="user",
        fallback_key_filename=".stash_key",
    )
    key = derive_key(cfg, tmp_path)
    assert stashed.get("key") == key
    assert stashed["service"] == "svc"
    assert stashed["username"] == "user"

    # Second call on an existing file should not re-stash.
    stashed.clear()
    key2 = derive_key(cfg, tmp_path)
    assert key2 == key
    assert stashed == {}


def test_memory_vault_write_read_round_trip(tmp_path: Path):
    key = os.urandom(KEY_BYTES)
    vault = MemoryVault(path=tmp_path / "renee.vault", key=key)
    vault.write(b"secrets go here")
    assert vault.read() == b"secrets go here"


def test_memory_vault_associated_data_mismatch_fails(tmp_path: Path):
    key = os.urandom(KEY_BYTES)
    vault = MemoryVault(path=tmp_path / "renee.vault", key=key)
    vault.write(b"payload", associated_data=b"ctx-a")
    with pytest.raises(Exception):
        vault.read(associated_data=b"ctx-b")
