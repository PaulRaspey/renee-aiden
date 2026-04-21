"""Tests for the dead-agent registry (patch 3)."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.identity.uahp_identity import create_identity
from src.uahp.dead_agent_registry import (
    DeadAgentRegistry,
    HeartbeatRejectedPostMortem,
)
from src.uahp.death_certs import DeathCause, issue_death_certificate


def _cert(agent_id: str):
    ident = create_identity(agent_id)
    return issue_death_certificate(
        ident, task_id="shutdown", cause=DeathCause.VOLUNTARY_SHUTDOWN
    )


def test_mark_dead_once_agent_no_longer_alive(tmp_path: Path):
    reg = DeadAgentRegistry(tmp_path / "reg.db")
    assert reg.is_alive("renee_voice") is True
    reg.mark_dead("renee_voice", _cert("renee_voice"))
    assert reg.is_alive("renee_voice") is False


def test_mark_dead_twice_is_idempotent(tmp_path: Path):
    reg = DeadAgentRegistry(tmp_path / "reg.db")
    reg.mark_dead("renee_voice", _cert("renee_voice"))
    # Second call with a *different* cert must not raise and must not revive.
    reg.mark_dead("renee_voice", _cert("renee_voice"))
    assert reg.is_alive("renee_voice") is False


def test_accept_heartbeat_for_alive_agent_no_exception(tmp_path: Path):
    reg = DeadAgentRegistry(tmp_path / "reg.db")
    # Doesn't raise for a brand-new agent_id.
    reg.accept_heartbeat("renee_memory", {"ts": 1.0, "seq": 1})


def test_accept_heartbeat_for_dead_agent_raises(tmp_path: Path):
    reg = DeadAgentRegistry(tmp_path / "reg.db")
    reg.mark_dead("renee_voice", _cert("renee_voice"))
    with pytest.raises(HeartbeatRejectedPostMortem) as exc_info:
        reg.accept_heartbeat("renee_voice", {"ts": 1.0, "seq": 1})
    assert exc_info.value.agent_id == "renee_voice"
    assert "renee_voice" in str(exc_info.value)


def test_registry_survives_restart(tmp_path: Path):
    db = tmp_path / "reg.db"
    reg = DeadAgentRegistry(db)
    reg.mark_dead("renee_voice", _cert("renee_voice"))
    del reg  # drop the first instance
    # Re-open pointing at the same DB.
    reg2 = DeadAgentRegistry(db)
    assert reg2.is_alive("renee_voice") is False
    with pytest.raises(HeartbeatRejectedPostMortem):
        reg2.accept_heartbeat("renee_voice", {})


def test_multiple_agents_tracked_independently(tmp_path: Path):
    reg = DeadAgentRegistry(tmp_path / "reg.db")
    reg.mark_dead("agent_a", _cert("agent_a"))
    # B still alive; heartbeats from B must still be accepted.
    assert reg.is_alive("agent_a") is False
    assert reg.is_alive("agent_b") is True
    reg.accept_heartbeat("agent_b", {})
    with pytest.raises(HeartbeatRejectedPostMortem):
        reg.accept_heartbeat("agent_a", {})
