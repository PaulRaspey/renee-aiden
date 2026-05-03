"""Tests for renee.secrets (#6).

The keyring package is optional; tests cover both paths — keyring-available
(via mock) and keyring-unavailable (real os, no keyring on the test box).
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from renee import secrets


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    """Make sure stray env vars from the host don't leak into tests."""
    for name in secrets.KNOWN_SECRETS:
        monkeypatch.delenv(name, raising=False)
    yield


def _fake_keyring(stored: dict):
    """Build a fake keyring module backed by a dict."""
    kr = MagicMock()
    kr.get_password.side_effect = lambda svc, name: stored.get((svc, name))

    def setp(svc, name, val):
        stored[(svc, name)] = val
    kr.set_password.side_effect = setp

    def delp(svc, name):
        if (svc, name) not in stored:
            raise RuntimeError("not stored")
        del stored[(svc, name)]
    kr.delete_password.side_effect = delp
    return kr


def test_get_returns_none_when_neither_env_nor_keyring(monkeypatch):
    monkeypatch.setattr(secrets, "_keyring", lambda: None)
    assert secrets.get("RUNPOD_API_KEY") is None


def test_get_reads_from_env_when_keyring_unavailable(monkeypatch):
    monkeypatch.setattr(secrets, "_keyring", lambda: None)
    monkeypatch.setenv("RUNPOD_API_KEY", "env-value")
    assert secrets.get("RUNPOD_API_KEY") == "env-value"


def test_get_prefers_keyring_over_env(monkeypatch):
    stored = {(secrets.SERVICE_NAME, "RUNPOD_API_KEY"): "kr-value"}
    monkeypatch.setattr(secrets, "_keyring", lambda: _fake_keyring(stored))
    monkeypatch.setenv("RUNPOD_API_KEY", "env-value")
    assert secrets.get("RUNPOD_API_KEY") == "kr-value"


def test_get_falls_back_to_env_when_keyring_lookup_throws(monkeypatch):
    kr = MagicMock()
    kr.get_password.side_effect = RuntimeError("backend unavailable")
    monkeypatch.setattr(secrets, "_keyring", lambda: kr)
    monkeypatch.setenv("RUNPOD_API_KEY", "env-value")
    assert secrets.get("RUNPOD_API_KEY") == "env-value"


def test_set_returns_false_without_keyring(monkeypatch):
    monkeypatch.setattr(secrets, "_keyring", lambda: None)
    assert secrets.set_("RUNPOD_API_KEY", "x") is False


def test_set_writes_to_keyring(monkeypatch):
    stored = {}
    monkeypatch.setattr(secrets, "_keyring", lambda: _fake_keyring(stored))
    assert secrets.set_("RUNPOD_API_KEY", "secret-1") is True
    assert stored == {(secrets.SERVICE_NAME, "RUNPOD_API_KEY"): "secret-1"}


def test_migrate_skips_already_in_keyring(monkeypatch):
    stored = {(secrets.SERVICE_NAME, "RUNPOD_API_KEY"): "existing"}
    monkeypatch.setattr(secrets, "_keyring", lambda: _fake_keyring(stored))
    monkeypatch.setenv("RUNPOD_API_KEY", "env-version")
    monkeypatch.setenv("GROQ_API_KEY", "groq-env")
    summary = secrets.migrate_env_to_keyring()
    assert "already in keyring" in summary["RUNPOD_API_KEY"]
    assert summary["GROQ_API_KEY"] == "migrated"
    # Existing keyring value preserved
    assert stored[(secrets.SERVICE_NAME, "RUNPOD_API_KEY")] == "existing"
    assert stored[(secrets.SERVICE_NAME, "GROQ_API_KEY")] == "groq-env"


def test_migrate_skips_when_keyring_unavailable(monkeypatch):
    monkeypatch.setattr(secrets, "_keyring", lambda: None)
    monkeypatch.setenv("RUNPOD_API_KEY", "x")
    summary = secrets.migrate_env_to_keyring()
    assert "keyring unavailable" in summary["RUNPOD_API_KEY"]


def test_migrate_dry_run_does_not_write(monkeypatch):
    stored = {}
    monkeypatch.setattr(secrets, "_keyring", lambda: _fake_keyring(stored))
    monkeypatch.setenv("RUNPOD_API_KEY", "x")
    summary = secrets.migrate_env_to_keyring(dry_run=True)
    assert summary["RUNPOD_API_KEY"] == "would migrate"
    assert stored == {}  # nothing actually written


def test_populate_env_loads_from_keyring(monkeypatch):
    stored = {(secrets.SERVICE_NAME, "RUNPOD_API_KEY"): "kr-1"}
    monkeypatch.setattr(secrets, "_keyring", lambda: _fake_keyring(stored))
    summary = secrets.populate_env_from_keyring()
    import os
    assert os.environ.get("RUNPOD_API_KEY") == "kr-1"
    assert summary["RUNPOD_API_KEY"] == "loaded"


def test_populate_env_does_not_clobber_existing(monkeypatch):
    stored = {(secrets.SERVICE_NAME, "RUNPOD_API_KEY"): "kr-value"}
    monkeypatch.setattr(secrets, "_keyring", lambda: _fake_keyring(stored))
    monkeypatch.setenv("RUNPOD_API_KEY", "env-already-set")
    summary = secrets.populate_env_from_keyring()
    import os
    # Existing env value wins
    assert os.environ["RUNPOD_API_KEY"] == "env-already-set"
    assert "skipped" in summary["RUNPOD_API_KEY"]


def test_populate_env_skips_unknown_secrets_silently(monkeypatch):
    monkeypatch.setattr(secrets, "_keyring", lambda: _fake_keyring({}))
    summary = secrets.populate_env_from_keyring()
    # All KNOWN_SECRETS are reported, none "loaded"
    for name in secrets.KNOWN_SECRETS:
        assert name in summary
        assert "skipped" in summary[name] or summary[name] == "loaded"
