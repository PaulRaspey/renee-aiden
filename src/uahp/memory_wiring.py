"""MemoryVault to UAHP wiring (MiniMax patch 5).

Ties MemoryStore state to the agent's signing identity:

    emit_memory_snapshot   signs a session-start snapshot (memory_count,
                           latest_memory_hash, tier_distribution) that
                           other agents use to verify continuity.
    attach_memory_proof    signs a continuity proof attached to a
                           CompletionReceipt, so downstream verifiers can
                           confirm the producing agent actually had a
                           specific memory state at receipt time.
    seal_memory_to_death   folds the current memory state into a death
                           certificate dict and re-signs it, giving
                           auditors a sealed record of what the agent knew
                           at death.

Verified against the real MemoryStore schema in src/memory/store.py: the
`memories` table has a `tier` column and the rows per tier are the fingerprint
used in tier_distribution. `recent_turns` returns dicts keyed on `user` and
`assistant`, which the snapshot and proof hash for continuity.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid


def emit_memory_snapshot(
    memory_store,
    agent_identity,
    session_id: str | None = None,
) -> dict:
    """Sign a memory identity snapshot for the current session.

    The snapshot fingerprints total memory count, the most recent assistant
    turn, and the per-tier breakdown so peers can spot continuity breaks
    without copying the memory corpus itself.
    """
    session_id = session_id or f"session-{uuid.uuid4().hex[:12]}"
    memory_count = memory_store.count()

    recent = memory_store.recent_turns(n=1)
    if recent:
        latest_hash = hashlib.sha256(
            (recent[0].get("assistant") or "").encode()
        ).hexdigest()[:16]
    else:
        latest_hash = "none"

    with sqlite3.connect(memory_store.db_path) as con:
        tier_rows = con.execute(
            "SELECT tier, COUNT(*) FROM memories GROUP BY tier"
        ).fetchall()
    tier_dist = {tier: count for tier, count in tier_rows}

    timestamp = time.time()
    payload = json.dumps(
        {
            "session_id": session_id,
            "agent_id": agent_identity.agent_id,
            "timestamp": timestamp,
            "memory_count": memory_count,
            "latest_memory_hash": latest_hash,
            "tier_distribution": tier_dist,
        },
        sort_keys=True,
    )
    signature = agent_identity.sign(payload)

    return {
        "session_id": session_id,
        "agent_id": agent_identity.agent_id,
        "timestamp": timestamp,
        "memory_count": memory_count,
        "latest_memory_hash": latest_hash,
        "tier_distribution": tier_dist,
        "signature": signature,
    }


def verify_memory_snapshot(agent_identity, snapshot: dict) -> bool:
    """Return True iff the snapshot was signed by the claimed identity."""
    payload = json.dumps(
        {
            "session_id": snapshot["session_id"],
            "agent_id": snapshot["agent_id"],
            "timestamp": snapshot["timestamp"],
            "memory_count": snapshot["memory_count"],
            "latest_memory_hash": snapshot["latest_memory_hash"],
            "tier_distribution": snapshot["tier_distribution"],
        },
        sort_keys=True,
    )
    return agent_identity.verify(payload, snapshot["signature"])


def attach_memory_proof(
    memory_store,
    agent_identity,
    receipt_id: str,
) -> dict:
    """Sign a memory-continuity proof attached to a CompletionReceipt.

    Fingerprints the most recent 3 assistant outputs as a context hash,
    so a verifier can spot missing or rewritten memory without getting the
    content itself.
    """
    memory_count = memory_store.count()
    recent = memory_store.recent_turns(n=3)

    context_chunks = [
        t["assistant"] for t in recent if t.get("assistant")
    ]
    context_hash = (
        hashlib.sha256("|".join(context_chunks).encode()).hexdigest()[:24]
        if context_chunks
        else "none"
    )

    timestamp = time.time()
    payload = json.dumps(
        {
            "receipt_id": receipt_id,
            "agent_id": agent_identity.agent_id,
            "memory_count_at_receipt": memory_count,
            "context_hash": context_hash,
            "timestamp": timestamp,
        },
        sort_keys=True,
    )
    signature = agent_identity.sign(payload)

    return {
        "receipt_id": receipt_id,
        "agent_id": agent_identity.agent_id,
        "memory_count_at_receipt": memory_count,
        "context_hash": context_hash,
        "timestamp": timestamp,
        "signature": signature,
    }


def verify_memory_proof(agent_identity, proof: dict) -> bool:
    """Return True iff the proof was signed by the claimed identity."""
    payload = json.dumps(
        {
            "receipt_id": proof["receipt_id"],
            "agent_id": proof["agent_id"],
            "memory_count_at_receipt": proof["memory_count_at_receipt"],
            "context_hash": proof["context_hash"],
            "timestamp": proof["timestamp"],
        },
        sort_keys=True,
    )
    return agent_identity.verify(payload, proof["signature"])


def seal_memory_to_death(
    memory_store,
    agent_identity,
    death_cert_dict: dict,
) -> dict:
    """Attach a memory_seal block to a death-cert dict and re-sign.

    Produces a new dict — callers should treat the return value as the
    sealed cert; the input is not mutated. The re-signed payload excludes
    the prior `signature` field so the seal stands on its own.
    """
    memory_count = memory_store.count()
    recent = memory_store.recent_turns(n=10)

    memory_summary = "|".join(
        f"{t['user'][:40]}->{t['assistant'][:40]}"
        for t in recent
        if t.get("user") and t.get("assistant")
    )
    state_hash = (
        hashlib.sha256(memory_summary.encode()).hexdigest()[:32]
        if memory_summary
        else "none"
    )

    sealed = dict(death_cert_dict)
    sealed["memory_seal"] = {
        "memory_count": memory_count,
        "state_hash": state_hash,
        "sealed_at": time.time(),
    }

    payload = json.dumps(
        {k: v for k, v in sealed.items() if k != "signature"},
        sort_keys=True,
    )
    sealed["signature"] = agent_identity.sign(payload)

    return sealed


def verify_sealed_death(agent_identity, sealed: dict) -> bool:
    """Verify a sealed (memory-bound) death cert dict."""
    payload = json.dumps(
        {k: v for k, v in sealed.items() if k != "signature"},
        sort_keys=True,
    )
    return agent_identity.verify(payload, sealed["signature"])
