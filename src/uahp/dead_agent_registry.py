"""Dead-agent registry with post-death heartbeat rejection (MiniMax patch 3).

Supervisors hold an instance of DeadAgentRegistry and call accept_heartbeat()
on every inbound heartbeat. Once an agent has been marked dead — either via
its own signed death certificate or a supervisor-issued one — further
heartbeats from that agent_id raise HeartbeatRejectedPostMortem. This closes
the zombie-revival loophole where a crashed worker comes back and tries to
resume as if nothing happened.

Backed by SQLite so the dead set survives process restarts.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from src.uahp.death_certs import DeathCertificate


class HeartbeatRejectedPostMortem(Exception):
    """Raised when a supervisor receives a heartbeat from an already-dead agent."""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        super().__init__(
            f"Heartbeat rejected: agent '{agent_id}' has declared death "
            f"and will not be revived."
        )


class DeadAgentRegistry:
    """SQLite-backed registry of agents that have declared death.

    Usage:
        registry = DeadAgentRegistry(state_dir / "dead_agent_registry.db")
        registry.mark_dead("renee_voice", cert)
        registry.is_alive("renee_voice")  # False
        registry.accept_heartbeat("renee_voice", {...})  # raises
    """

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._dead: set[str] = set(self._load_dead())

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS dead_agents (
                    agent_id TEXT PRIMARY KEY,
                    death_id TEXT NOT NULL,
                    death_timestamp REAL NOT NULL,
                    death_cert_json TEXT NOT NULL,
                    marked_at REAL NOT NULL
                )
                """
            )

    def _load_dead(self) -> list[str]:
        with sqlite3.connect(self.db_path) as con:
            rows = con.execute("SELECT agent_id FROM dead_agents").fetchall()
        return [r[0] for r in rows]

    def is_alive(self, agent_id: str) -> bool:
        """Return False once the agent has been marked dead."""
        return agent_id not in self._dead

    def mark_dead(self, agent_id: str, death_cert: DeathCertificate) -> None:
        """Register the agent as dead. Idempotent: second call is a no-op."""
        if agent_id in self._dead:
            return
        self._dead.add(agent_id)
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                INSERT OR REPLACE INTO dead_agents
                    (agent_id, death_id, death_timestamp, death_cert_json, marked_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    death_cert.death_id,
                    death_cert.timestamp,
                    json.dumps(death_cert.to_dict()),
                    time.time(),
                ),
            )

    def accept_heartbeat(self, agent_id: str, heartbeat: Any) -> None:
        """Accept a heartbeat. Raises HeartbeatRejectedPostMortem if dead."""
        if agent_id in self._dead:
            raise HeartbeatRejectedPostMortem(agent_id)
