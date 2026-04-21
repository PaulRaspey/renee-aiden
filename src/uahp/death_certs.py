"""UAHP death certificates with task_id and cause (MiniMax patch 1).

Extends the receipt-based shutdown path in `renee.shutdown` with a richer,
standalone death certificate: each cert records *why* the agent died
(DeathCause enum) and the task that was in flight when it happened. The
certificate is signed by the agent's own AgentIdentity so any verifier with
the matching identity can prove authenticity and detect tamper.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from src.identity.uahp_identity import AgentIdentity


class DeathCause(str, Enum):
    NATURAL = "natural"
    VOLUNTARY_SHUTDOWN = "voluntary_shutdown"
    SUPERVISOR_TERMINATED = "supervisor_terminated"
    HEARTBEAT_TIMEOUT = "heartbeat_timeout"
    TASK_FAILURE = "task_failure"
    HARDWARE_FAULT = "hardware_fault"
    SEGFAULT = "segfault"
    OOM = "oom"
    UNKNOWN = "unknown"


@dataclass
class DeathCertificate:
    agent_id: str
    death_id: str
    task_id: str
    cause: DeathCause
    timestamp: float
    last_receipt_id: str | None
    signature: str
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "death_id": self.death_id,
            "task_id": self.task_id,
            "cause": self.cause.value,
            "timestamp": self.timestamp,
            "last_receipt_id": self.last_receipt_id,
            "signature": self.signature,
            "metadata": self.metadata,
        }


def _payload(
    agent_id: str,
    death_id: str,
    task_id: str,
    cause: DeathCause,
    timestamp: float,
    last_receipt_id: str | None,
) -> str:
    return json.dumps(
        {
            "agent_id": agent_id,
            "death_id": death_id,
            "task_id": task_id,
            "cause": cause.value,
            "timestamp": timestamp,
            "last_receipt_id": last_receipt_id,
        },
        sort_keys=True,
    )


def issue_death_certificate(
    identity: AgentIdentity,
    task_id: str = "unknown",
    cause: DeathCause = DeathCause.NATURAL,
    last_receipt_id: str | None = None,
    metadata: dict | None = None,
) -> DeathCertificate:
    """Sign a death certificate for the given agent."""
    death_id = f"death-{uuid.uuid4().hex[:12]}"
    timestamp = time.time()
    payload = _payload(
        identity.agent_id, death_id, task_id, cause, timestamp, last_receipt_id
    )
    signature = identity.sign(payload)
    return DeathCertificate(
        agent_id=identity.agent_id,
        death_id=death_id,
        task_id=task_id,
        cause=cause,
        timestamp=timestamp,
        last_receipt_id=last_receipt_id,
        signature=signature,
        metadata=metadata or {},
    )


def verify_death_certificate(identity: AgentIdentity, cert: DeathCertificate) -> bool:
    """Return True iff the certificate was signed by the given identity."""
    payload = _payload(
        cert.agent_id,
        cert.death_id,
        cert.task_id,
        cert.cause,
        cert.timestamp,
        cert.last_receipt_id,
    )
    return identity.verify(payload, cert.signature)
