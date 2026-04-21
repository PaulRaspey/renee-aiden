"""QAL attestation chain.

Quantum Attestation Lattice primitive for cross-session continuity. Each
attestation binds (agent_id, action, UTC timestamp, state_hash, prev_hash)
under the agent's signing identity. prev_hash links each attestation to the
full hash of the previous one, so any mutation in the middle of a chain
surfaces at the next verification step. Genesis attestations carry a
prev_hash of 64 zeros.

Complementary to memory_wiring.emit_memory_snapshot (which attests "what I
knew at this moment"): the QAL chain attests "this session follows that
session", giving Renée a cryptographic thread from session N back to
session 0 regardless of what happened to memory state in between.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.identity.uahp_identity import AgentIdentity


GENESIS_PREV_HASH = "0" * 64


class ChainLoadError(Exception):
    """Raised by load_chain when the file is missing, empty beyond repair,
    or contains a line that cannot be parsed as a JSON attestation."""


@dataclass
class Attestation:
    agent_id: str
    action: str
    timestamp: str  # ISO 8601 UTC
    state_hash: str
    prev_hash: str
    signature: str
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_state(state_blob: Any) -> str:
    """SHA-256 of the canonical JSON form of any JSON-serializable blob."""
    canonical = json.dumps(state_blob, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def hash_attestation(attestation: Attestation) -> str:
    """SHA-256 of the full attestation including signature. This is what
    the next attestation in the chain will reference as prev_hash — so
    tampering with any field (including the signature) surfaces at the
    next verification step."""
    canonical = json.dumps(attestation.to_dict(), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _signing_payload(
    agent_id: str,
    action: str,
    timestamp: str,
    state_hash: str,
    prev_hash: str,
) -> str:
    return json.dumps(
        {
            "agent_id": agent_id,
            "action": action,
            "timestamp": timestamp,
            "state_hash": state_hash,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
    )


def create_genesis(
    agent_identity: AgentIdentity,
    state_blob: Any,
    action_descriptor: str,
    metadata: dict | None = None,
    timestamp: str | None = None,
) -> Attestation:
    """Mint the first attestation in a chain. prev_hash is 64 zero chars."""
    ts = timestamp or _utc_now_iso()
    state_hash = hash_state(state_blob)
    payload = _signing_payload(
        agent_identity.agent_id,
        action_descriptor,
        ts,
        state_hash,
        GENESIS_PREV_HASH,
    )
    signature = agent_identity.sign(payload)
    return Attestation(
        agent_id=agent_identity.agent_id,
        action=action_descriptor,
        timestamp=ts,
        state_hash=state_hash,
        prev_hash=GENESIS_PREV_HASH,
        signature=signature,
        metadata=metadata or {},
    )


def append(
    prev_attestation: Attestation,
    agent_identity: AgentIdentity,
    state_blob: Any,
    action_descriptor: str,
    metadata: dict | None = None,
    timestamp: str | None = None,
) -> Attestation:
    """Mint the next attestation, linking it to prev via prev_hash."""
    ts = timestamp or _utc_now_iso()
    state_hash = hash_state(state_blob)
    prev_hash = hash_attestation(prev_attestation)
    payload = _signing_payload(
        agent_identity.agent_id,
        action_descriptor,
        ts,
        state_hash,
        prev_hash,
    )
    signature = agent_identity.sign(payload)
    return Attestation(
        agent_id=agent_identity.agent_id,
        action=action_descriptor,
        timestamp=ts,
        state_hash=state_hash,
        prev_hash=prev_hash,
        signature=signature,
        metadata=metadata or {},
    )


def verify_attestation(
    agent_identity: AgentIdentity, attestation: Attestation
) -> bool:
    """Return True iff the attestation's signature matches the identity."""
    payload = _signing_payload(
        attestation.agent_id,
        attestation.action,
        attestation.timestamp,
        attestation.state_hash,
        attestation.prev_hash,
    )
    return agent_identity.verify(payload, attestation.signature)


def _resolve_identity(
    identities: AgentIdentity | Mapping[str, AgentIdentity] | None,
    agent_id: str,
) -> AgentIdentity | None:
    if identities is None:
        return None
    if isinstance(identities, AgentIdentity):
        return identities
    return identities.get(agent_id)


def verify_chain(
    attestations: list[Attestation],
    identities: AgentIdentity | Mapping[str, AgentIdentity] | None = None,
) -> bool:
    """Return True iff:
      - every prev_hash links correctly to the previous attestation (or
        GENESIS_PREV_HASH at index 0), AND
      - every signature verifies under the supplied identity.

    Empty or single-attestation chains pass (vacuously for empty; the
    single attestation is verified if identities is provided).

    identities may be:
      - None: only check prev-hash links (skip signature check).
      - AgentIdentity: used to verify every attestation (single-agent chain).
      - Mapping[agent_id -> AgentIdentity]: used to look up per attestation.
    """
    return find_tamper(attestations, identities) is None


def find_tamper(
    attestations: list[Attestation],
    identities: AgentIdentity | Mapping[str, AgentIdentity] | None = None,
) -> int | None:
    """Return the index of the first tampered attestation, or None if clean."""
    expected_prev = GENESIS_PREV_HASH
    for i, a in enumerate(attestations):
        if a.prev_hash != expected_prev:
            return i
        identity = _resolve_identity(identities, a.agent_id)
        if identity is not None and not verify_attestation(identity, a):
            return i
        expected_prev = hash_attestation(a)
    return None


def cross_chain_collision_report(
    chain_a: Iterable[Attestation],
    chain_b: Iterable[Attestation],
) -> list[dict]:
    """Report state_hash collisions between two chains.

    Returns a list of {state_hash, idx_in_a, idx_in_b} for any state_hash
    that appears in both chains. Collisions are a flag for auditors, not an
    error — two agents can legitimately hash the same state.
    """
    a_by_hash: dict[str, int] = {}
    for i, a in enumerate(chain_a):
        a_by_hash.setdefault(a.state_hash, i)
    collisions: list[dict] = []
    for j, b in enumerate(chain_b):
        if b.state_hash in a_by_hash:
            collisions.append(
                {
                    "state_hash": b.state_hash,
                    "idx_in_a": a_by_hash[b.state_hash],
                    "idx_in_b": j,
                }
            )
    return collisions


def serialize_chain(attestations: Iterable[Attestation], path: Path | str) -> None:
    """Write the chain as JSONL — one attestation per line."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for a in attestations:
            fh.write(json.dumps(a.to_dict(), sort_keys=True) + "\n")


def load_chain(path: Path | str) -> list[Attestation]:
    """Read a JSONL chain back. Raises ChainLoadError with a clear message
    on a corrupt or missing file — never a raw JSONDecodeError."""
    path = Path(path)
    if not path.exists():
        raise ChainLoadError(f"chain file not found: {path}")
    chain: list[Attestation] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as e:
                raise ChainLoadError(
                    f"corrupt JSONL at line {lineno} in {path}: {e.msg}"
                ) from None
            try:
                chain.append(
                    Attestation(
                        agent_id=data["agent_id"],
                        action=data["action"],
                        timestamp=data["timestamp"],
                        state_hash=data["state_hash"],
                        prev_hash=data["prev_hash"],
                        signature=data["signature"],
                        metadata=data.get("metadata", {}),
                    )
                )
            except KeyError as e:
                raise ChainLoadError(
                    f"missing field {e.args[0]!r} at line {lineno} in {path}"
                ) from None
    return chain
