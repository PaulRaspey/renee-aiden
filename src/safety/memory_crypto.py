"""
Memory encryption (M13 / SAFETY.md §Memory Store Encryption).

AES-256-GCM content encryption. Key derivation tries, in order:
  1. `keyring` (Windows Credential Manager on PJ's box)
  2. A 32-byte key file in the state dir (mode 0o600 where the OS respects it)

Both paths produce a 32-byte raw key; the API surface is identical from
the caller's perspective.

`MemoryVault` is a thin wrapper that encrypts/decrypts bytes given a
path. It does NOT replace the SQLite memory store — it's intended for
opt-in row-level encryption or full-file backups. Enable via
`safety.memory_encryption.enabled: true` in `configs/safety.yaml`.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover
    AESGCM = None  # type: ignore

from .config import MemoryEncryptionConfig


KEY_BYTES = 32          # AES-256
NONCE_BYTES = 12        # recommended for GCM
MAGIC = b"RNGM1"        # Renée memory, v1 — sanity header on encrypted blobs


class CryptoUnavailable(RuntimeError):
    """Raised when cryptography isn't installed and crypto is actually needed."""


def _require_cryptography() -> None:
    if AESGCM is None:  # pragma: no cover
        raise CryptoUnavailable(
            "The `cryptography` package is required for memory encryption. "
            "`pip install cryptography`."
        )


# -------------------- key derivation --------------------


def _try_keyring_get(service: str, username: str) -> Optional[bytes]:
    try:
        import keyring  # type: ignore
    except Exception:
        return None
    try:
        raw = keyring.get_password(service, username)
    except Exception:
        return None
    if not raw:
        return None
    # Stored as hex for portability.
    try:
        val = bytes.fromhex(raw)
    except ValueError:
        return None
    if len(val) != KEY_BYTES:
        return None
    return val


def _try_keyring_set(service: str, username: str, key: bytes) -> bool:
    try:
        import keyring  # type: ignore
    except Exception:
        return False
    try:
        keyring.set_password(service, username, key.hex())
        return True
    except Exception:
        return False


def _load_or_create_key_file(path: Path) -> bytes:
    if path.exists():
        data = path.read_bytes()
        if len(data) == KEY_BYTES:
            return data
        # File corrupt or wrong length; regenerate and overwrite.
    key = secrets.token_bytes(KEY_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    # Best-effort permission tighten (Windows only honors flags loosely).
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def derive_key(cfg: MemoryEncryptionConfig, state_dir: str | Path) -> bytes:
    """
    Return a 32-byte key. Prefer keyring; if keyring is unavailable or
    empty, fall back to a state-dir key file (auto-created on first use).
    If the key file had to be created, also try to stash it in keyring
    for next time.
    """
    key = _try_keyring_get(cfg.keyring_service, cfg.keyring_username)
    if key is not None:
        return key
    state_dir_p = Path(state_dir)
    fallback = state_dir_p / cfg.fallback_key_filename
    existed = fallback.exists()
    key = _load_or_create_key_file(fallback)
    if not existed:
        # Keyring missed; if we just minted a fresh key, attempt to store it.
        _try_keyring_set(cfg.keyring_service, cfg.keyring_username, key)
    return key


# -------------------- encrypt / decrypt --------------------


def encrypt(plaintext: bytes, key: bytes, *, associated_data: bytes = b"") -> bytes:
    _require_cryptography()
    if len(key) != KEY_BYTES:
        raise ValueError(f"key must be {KEY_BYTES} bytes (AES-256), got {len(key)}")
    nonce = secrets.token_bytes(NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext, associated_data or None)
    return MAGIC + nonce + ct


def decrypt(blob: bytes, key: bytes, *, associated_data: bytes = b"") -> bytes:
    _require_cryptography()
    if len(key) != KEY_BYTES:
        raise ValueError(f"key must be {KEY_BYTES} bytes (AES-256), got {len(key)}")
    if not blob.startswith(MAGIC):
        raise ValueError("blob missing magic header; not a Renée memory ciphertext")
    stripped = blob[len(MAGIC):]
    if len(stripped) < NONCE_BYTES + 16:
        raise ValueError("blob too short for nonce + tag")
    nonce = stripped[:NONCE_BYTES]
    ct = stripped[NONCE_BYTES:]
    return AESGCM(key).decrypt(nonce, ct, associated_data or None)


@dataclass
class MemoryVault:
    """
    Path-scoped encrypt/decrypt helper. One vault per file; key is bound
    at construction so the caller doesn't pass it around.
    """
    path: Path
    key: bytes

    def write(self, plaintext: bytes, *, associated_data: bytes = b"") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        blob = encrypt(plaintext, self.key, associated_data=associated_data)
        self.path.write_bytes(blob)

    def read(self, *, associated_data: bytes = b"") -> bytes:
        return decrypt(self.path.read_bytes(), self.key, associated_data=associated_data)
