"""UAHP task-failure certificates (MiniMax patch 2).

Complements death certificates: a task-failure cert records a specific task
that failed, before or after the owning agent declares death. Error-code is
machine-readable (OOM, TIMEOUT, SEGFAULT, ...) so downstream schedulers can
classify failures without parsing error_message.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

from src.identity.uahp_identity import AgentIdentity


@dataclass
class TaskFailureCertificate:
    cert_id: str
    agent_id: str
    task_id: str
    error_message: str
    error_code: str
    timestamp: float
    signature: str
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "cert_id": self.cert_id,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "error_message": self.error_message,
            "error_code": self.error_code,
            "timestamp": self.timestamp,
            "signature": self.signature,
            "metadata": self.metadata,
        }


def _payload(
    cert_id: str,
    agent_id: str,
    task_id: str,
    error_message: str,
    error_code: str,
    timestamp: float,
) -> str:
    return json.dumps(
        {
            "cert_id": cert_id,
            "agent_id": agent_id,
            "task_id": task_id,
            "error_message": error_message,
            "error_code": error_code,
            "timestamp": timestamp,
        },
        sort_keys=True,
    )


def issue_task_failure_certificate(
    identity: AgentIdentity,
    task_id: str,
    error_message: str,
    error_code: str = "UNKNOWN",
    metadata: dict | None = None,
) -> TaskFailureCertificate:
    """Sign a task-failure certificate for the given agent."""
    cert_id = f"tfail-{uuid.uuid4().hex[:12]}"
    timestamp = time.time()
    payload = _payload(
        cert_id, identity.agent_id, task_id, error_message, error_code, timestamp
    )
    signature = identity.sign(payload)
    return TaskFailureCertificate(
        cert_id=cert_id,
        agent_id=identity.agent_id,
        task_id=task_id,
        error_message=error_message,
        error_code=error_code,
        timestamp=timestamp,
        signature=signature,
        metadata=metadata or {},
    )


def verify_task_failure_certificate(
    identity: AgentIdentity, cert: TaskFailureCertificate
) -> bool:
    """Return True iff the certificate was signed by the given identity."""
    payload = _payload(
        cert.cert_id,
        cert.agent_id,
        cert.task_id,
        cert.error_message,
        cert.error_code,
        cert.timestamp,
    )
    return identity.verify(payload, cert.signature)
