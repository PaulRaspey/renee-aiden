"""Tests for MemoryVault-UAHP wiring (patch 5).

Schema verified against the real MemoryStore (src/memory/store.py): the
`memories` table has a `tier` column and `recent_turns` returns dicts keyed
on `user` / `assistant`. The fixture inserts rows via raw SQL so embeddings
are never computed (keeps tests off the HuggingFace network path that
test_memory.py already owns).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest

from src.identity.uahp_identity import create_identity
from src.memory.store import MemoryStore
from src.uahp.death_certs import DeathCause, issue_death_certificate
from src.uahp.memory_wiring import (
    attach_memory_proof,
    emit_memory_snapshot,
    seal_memory_to_death,
    verify_memory_proof,
    verify_memory_snapshot,
    verify_sealed_death,
)


@pytest.fixture
def memory_store(tmp_path: Path) -> MemoryStore:
    """MemoryStore with no extractor — write_turn() logs turns without
    hitting the embedding backend, so no sentence-transformers load."""
    return MemoryStore(persona_name="test", state_dir=tmp_path)


def _insert_raw_memory(store: MemoryStore, content: str, tier: str = "casual") -> None:
    """Insert a memories row directly via SQL so tests don't touch the
    embedding backend. Embedding blob is zeros; tests here only care about
    count() and tier distribution."""
    zeros = np.zeros(store.embedding.dim, dtype="float32").tobytes()
    with sqlite3.connect(store.db_path) as con:
        con.execute(
            "INSERT INTO memories (id,content,embedding,emotional_valence,"
            "emotional_intensity,salience,tier,created_at,last_referenced,"
            "reference_count,source_turn_id,tags,contextual_triggers) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"mem-{content[:10]}-{tier}",
                content,
                zeros,
                0.0,
                0.3,
                0.5,
                tier,
                1.0,
                1.0,
                0,
                "fixture",
                "[]",
                "[]",
            ),
        )


def test_snapshot_roundtrip(memory_store: MemoryStore):
    ident = create_identity("renee_memory")
    snap = emit_memory_snapshot(memory_store, ident, session_id="s-001")
    assert verify_memory_snapshot(ident, snap) is True
    assert snap["session_id"] == "s-001"
    assert snap["agent_id"] == "renee_memory"
    assert snap["memory_count"] == 0
    assert snap["latest_memory_hash"] == "none"
    assert snap["tier_distribution"] == {}


def test_snapshot_tamper_rejected(memory_store: MemoryStore):
    ident = create_identity("renee_memory")
    snap = emit_memory_snapshot(memory_store, ident)
    snap["memory_count"] = 9999
    assert verify_memory_snapshot(ident, snap) is False


def test_snapshot_cross_agent_forgery_rejected(memory_store: MemoryStore):
    alice = create_identity("alice")
    bob = create_identity("bob")
    snap = emit_memory_snapshot(memory_store, alice)
    assert verify_memory_snapshot(bob, snap) is False


def test_snapshot_captures_actual_memory_count(memory_store: MemoryStore):
    _insert_raw_memory(memory_store, "first", tier="casual")
    _insert_raw_memory(memory_store, "second", tier="casual")
    _insert_raw_memory(memory_store, "third", tier="core")
    ident = create_identity("renee_memory")
    snap = emit_memory_snapshot(memory_store, ident)
    assert snap["memory_count"] == 3
    assert snap["tier_distribution"] == {"casual": 2, "core": 1}
    assert verify_memory_snapshot(ident, snap) is True


def test_snapshot_latest_hash_reflects_recent_turn(memory_store: MemoryStore):
    memory_store.write_turn("hi", "hello there", mood=None)
    ident = create_identity("renee_memory")
    snap = emit_memory_snapshot(memory_store, ident)
    assert snap["latest_memory_hash"] != "none"
    assert len(snap["latest_memory_hash"]) == 16


def test_memory_proof_roundtrip(memory_store: MemoryStore):
    memory_store.write_turn("hi", "hello", mood=None)
    memory_store.write_turn("how are you", "doing fine", mood=None)
    ident = create_identity("renee_memory")
    proof = attach_memory_proof(memory_store, ident, receipt_id="receipt-xyz")
    assert verify_memory_proof(ident, proof) is True
    assert proof["receipt_id"] == "receipt-xyz"
    assert proof["memory_count_at_receipt"] == 0  # no memories extracted
    assert proof["context_hash"] != "none"


def test_memory_proof_tamper_rejected(memory_store: MemoryStore):
    ident = create_identity("renee_memory")
    proof = attach_memory_proof(memory_store, ident, receipt_id="r1")
    proof["context_hash"] = "deadbeef"
    assert verify_memory_proof(ident, proof) is False


def test_seal_memory_to_death_signs_with_memory_seal(memory_store: MemoryStore):
    memory_store.write_turn("hi", "hello", mood=None)
    _insert_raw_memory(memory_store, "important fact", tier="core")
    ident = create_identity("renee_memory")
    cert = issue_death_certificate(
        ident, task_id="shutdown", cause=DeathCause.VOLUNTARY_SHUTDOWN
    )
    sealed = seal_memory_to_death(memory_store, ident, cert.to_dict())
    assert "memory_seal" in sealed
    assert sealed["memory_seal"]["memory_count"] == 1
    assert sealed["memory_seal"]["state_hash"] != "none"
    # The re-signed sealed cert must verify under the same identity.
    assert verify_sealed_death(ident, sealed) is True


def test_seal_does_not_mutate_input_dict(memory_store: MemoryStore):
    ident = create_identity("renee_memory")
    cert = issue_death_certificate(ident, task_id="t", cause=DeathCause.NATURAL)
    original = cert.to_dict()
    original_copy = dict(original)
    sealed = seal_memory_to_death(memory_store, ident, original)
    assert "memory_seal" not in original
    assert original == original_copy
    assert sealed is not original
