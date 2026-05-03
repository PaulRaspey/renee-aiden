"""Secrets layer: keyring-backed with environment-variable fallback.

Today the OptiPlex reads RUNPOD_API_KEY / GROQ_API_KEY / ELEVENLABS_API_KEY
from a plaintext ``.env`` file. That's fine for a single dev box but
awkward across machines (sync the .env or re-paste each time) and exposes
the keys to anything that opens the file. The OS keyring (Windows
Credential Manager, macOS Keychain, Secret Service on Linux) handles
both problems.

This module exposes ``get(name)`` and ``set(name, value)`` that:
  1. Try the OS keyring first (requires the optional ``keyring`` pkg)
  2. Fall back to ``os.environ.get(name)`` so existing .env workflows
     keep working without any change
  3. Never raise on the keyring path — a missing keyring or backend
     failure simply degrades to env

Migration: ``migrate_env_to_keyring()`` walks the known secret names,
copies each from env into keyring (skipping ones already in keyring),
and prints a summary so Paul can wipe the values from .env afterward.
"""
from __future__ import annotations

import logging
import os
from typing import Optional


logger = logging.getLogger("renee.secrets")


# All secrets renee knows about. Add new ones here so migrate_env_to_keyring
# picks them up automatically.
KNOWN_SECRETS: tuple[str, ...] = (
    "RUNPOD_API_KEY",
    "GROQ_API_KEY",
    "ELEVENLABS_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "BRIDGE_TOKEN",
    "BEACON_URL",  # not strictly secret but identifies the deploy
    "TAILSCALE_AUTHKEY",
    "HF_TOKEN",
)


SERVICE_NAME = "renee-aiden"


def _keyring():
    """Lazy import keyring. Returns the module or None when unavailable."""
    try:
        import keyring  # type: ignore
        return keyring
    except Exception as e:
        logger.debug("keyring not available: %s", e)
        return None


def get(name: str) -> Optional[str]:
    """Read a secret. Prefers keyring; falls back to env."""
    kr = _keyring()
    if kr is not None:
        try:
            val = kr.get_password(SERVICE_NAME, name)
            if val is not None and val != "":
                return val
        except Exception as e:
            # Keyring backend can fail in headless / SSH'd / cron contexts.
            # Don't propagate — env fallback below covers it.
            logger.debug("keyring.get_password(%s) failed: %s", name, e)
    val = os.environ.get(name)
    return val if val else None


def set_(name: str, value: str) -> bool:
    """Store a secret in keyring. Returns False if keyring unavailable.

    Named ``set_`` to avoid shadowing the builtin in callers that
    ``from renee.secrets import set as set_secret``.
    """
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.set_password(SERVICE_NAME, name, value)
        return True
    except Exception as e:
        logger.warning("keyring.set_password(%s) failed: %s", name, e)
        return False


def delete(name: str) -> bool:
    """Remove a secret from keyring. Returns False if keyring unavailable
    or the secret didn't exist."""
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.delete_password(SERVICE_NAME, name)
        return True
    except Exception as e:
        # Most backends raise on delete-of-missing; that's not an error
        # from the caller's perspective.
        logger.debug("keyring.delete_password(%s) failed: %s", name, e)
        return False


def migrate_env_to_keyring(*, dry_run: bool = False) -> dict[str, str]:
    """Copy each KNOWN_SECRETS value from env into keyring.

    Skips secrets already present in keyring (so re-running is safe and
    won't clobber a value Paul changed in keyring). Returns a per-name
    status dict suitable for printing.

    With ``dry_run=True``, reports what would happen without writing.
    """
    kr = _keyring()
    result: dict[str, str] = {}
    for name in KNOWN_SECRETS:
        env_val = os.environ.get(name, "")
        if not env_val:
            result[name] = "skipped (not in env)"
            continue
        if kr is None:
            result[name] = "skipped (keyring unavailable)"
            continue
        try:
            existing = kr.get_password(SERVICE_NAME, name)
        except Exception:
            existing = None
        if existing:
            result[name] = "skipped (already in keyring)"
            continue
        if dry_run:
            result[name] = "would migrate"
            continue
        try:
            kr.set_password(SERVICE_NAME, name, env_val)
            result[name] = "migrated"
        except Exception as e:
            result[name] = f"failed: {e!r}"
    return result


def populate_env_from_keyring() -> dict[str, str]:
    """Read each KNOWN_SECRETS value from keyring and inject into
    ``os.environ`` if not already set there. Used at process startup so
    code that reads ``os.environ.get('GROQ_API_KEY')`` keeps working
    after migration without changing every call site.

    Returns a per-name status dict.
    """
    kr = _keyring()
    result: dict[str, str] = {}
    for name in KNOWN_SECRETS:
        if os.environ.get(name):
            result[name] = "skipped (env already set)"
            continue
        if kr is None:
            result[name] = "skipped (keyring unavailable)"
            continue
        try:
            val = kr.get_password(SERVICE_NAME, name)
        except Exception as e:
            result[name] = f"keyring error: {e!r}"
            continue
        if val:
            os.environ[name] = val
            result[name] = "loaded"
        else:
            result[name] = "skipped (not in keyring)"
    return result


__all__ = [
    "KNOWN_SECRETS", "SERVICE_NAME",
    "get", "set_", "delete",
    "migrate_env_to_keyring", "populate_env_from_keyring",
]
