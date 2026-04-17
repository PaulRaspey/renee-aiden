"""
UAHP-native identity for Renée/Aiden agents.

Adapted from PJ's uahp-stack/core.py pattern. The PyPI `uahp` 0.5.4 wheel
is broken (imports `.identity` that does not ship in the wheel), so we
implement the same trust primitives here: HMAC-SHA256 signing, public
hash verification, and signed completion receipts.

Every agent in the Renée stack (persona, mood, memory, eventually asr/tts)
carries one of these identities. Outputs that cross agent boundaries carry
a CompletionReceipt signed by the producing agent.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass
class AgentIdentity:
    agent_id: str
    signing_key: str
    public_hash: str
    created_at: float
    metadata: dict = field(default_factory=dict)

    def sign(self, message: str) -> str:
        return hmac.new(
            self.signing_key.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()

    def verify(self, message: str, signature: str) -> bool:
        expected = self.sign(message)
        return hmac.compare_digest(expected, signature)

    def to_public(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "public_hash": self.public_hash,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }

    def to_private(self) -> dict:
        return asdict(self)

    @classmethod
    def from_private(cls, data: dict) -> "AgentIdentity":
        return cls(**data)


@dataclass
class CompletionReceipt:
    receipt_id: str
    agent_id: str
    task_id: str
    action: str
    timestamp: float
    duration_ms: float
    success: bool
    input_hash: str
    output_hash: str
    signature: str
    metadata: dict = field(default_factory=dict)


def create_identity(agent_id: str, metadata: dict | None = None) -> AgentIdentity:
    signing_key = secrets.token_hex(32)
    public_hash = hashlib.sha256(signing_key.encode()).hexdigest()
    return AgentIdentity(
        agent_id=agent_id,
        signing_key=signing_key,
        public_hash=public_hash,
        created_at=time.time(),
        metadata=metadata or {},
    )


def load_or_create(
    agent_id: str,
    state_dir: Path,
    metadata: dict | None = None,
) -> AgentIdentity:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    keyfile = state_dir / f"{agent_id}.key.json"
    if keyfile.exists():
        return AgentIdentity.from_private(json.loads(keyfile.read_text()))
    identity = create_identity(agent_id, metadata)
    keyfile.write_text(json.dumps(identity.to_private(), indent=2))
    return identity


def _hash(obj: Any) -> str:
    if isinstance(obj, (bytes, bytearray)):
        payload = bytes(obj)
    elif isinstance(obj, str):
        payload = obj.encode()
    else:
        payload = json.dumps(obj, sort_keys=True, default=str).encode()
    return hashlib.sha256(payload).hexdigest()


def sign_receipt(
    identity: AgentIdentity,
    task_id: str,
    action: str,
    duration_ms: float,
    success: bool,
    input_data: Any,
    output_data: Any,
    metadata: dict | None = None,
) -> CompletionReceipt:
    input_hash = _hash(input_data)
    output_hash = _hash(output_data)
    receipt_id = f"receipt-{uuid.uuid4().hex[:12]}"
    timestamp = time.time()
    payload = json.dumps(
        {
            "receipt_id": receipt_id,
            "agent_id": identity.agent_id,
            "task_id": task_id,
            "action": action,
            "timestamp": timestamp,
            "duration_ms": duration_ms,
            "success": success,
            "input_hash": input_hash,
            "output_hash": output_hash,
        },
        sort_keys=True,
    )
    signature = identity.sign(payload)
    return CompletionReceipt(
        receipt_id=receipt_id,
        agent_id=identity.agent_id,
        task_id=task_id,
        action=action,
        timestamp=timestamp,
        duration_ms=duration_ms,
        success=success,
        input_hash=input_hash,
        output_hash=output_hash,
        signature=signature,
        metadata=metadata or {},
    )


def verify_receipt(identity: AgentIdentity, receipt: CompletionReceipt) -> bool:
    payload = json.dumps(
        {
            "receipt_id": receipt.receipt_id,
            "agent_id": receipt.agent_id,
            "task_id": receipt.task_id,
            "action": receipt.action,
            "timestamp": receipt.timestamp,
            "duration_ms": receipt.duration_ms,
            "success": receipt.success,
            "input_hash": receipt.input_hash,
            "output_hash": receipt.output_hash,
        },
        sort_keys=True,
    )
    return identity.verify(payload, receipt.signature)


class ReneeIdentityManager:
    """One-stop identity registry for every Renée/Aiden-stack agent."""

    AGENT_NAMES = [
        "renee_persona",
        "renee_memory",
        "renee_mood",
        "renee_voice",
        "renee_ears",
        "renee_paralinguistics",
        "aiden_persona",
        "aiden_memory",
        "aiden_mood",
        "aiden_voice",
        "aiden_ears",
        "aiden_paralinguistics",
        "shared_orchestrator",
        "shared_eval",
    ]

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir) / "identities"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, AgentIdentity] = {}

    def get(self, agent_name: str, metadata: dict | None = None) -> AgentIdentity:
        if agent_name in self._cache:
            return self._cache[agent_name]
        identity = load_or_create(agent_name, self.state_dir, metadata)
        self._cache[agent_name] = identity
        return identity

    def bootstrap_all(self) -> dict[str, AgentIdentity]:
        return {name: self.get(name) for name in self.AGENT_NAMES}
