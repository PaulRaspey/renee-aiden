"""Tests for the QAL attestation chain."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.identity.uahp_identity import create_identity
from src.uahp.qal_chain import (
    GENESIS_PREV_HASH,
    Attestation,
    ChainLoadError,
    append,
    create_genesis,
    cross_chain_collision_report,
    find_tamper,
    hash_attestation,
    load_chain,
    serialize_chain,
    verify_attestation,
    verify_chain,
)


def _build_chain(identity, length: int) -> list[Attestation]:
    chain = [create_genesis(identity, {"seq": 0}, f"step-0")]
    for i in range(1, length):
        chain.append(append(chain[-1], identity, {"seq": i}, f"step-{i}"))
    return chain


def test_genesis_has_all_zero_prev_hash():
    ident = create_identity("renee_persona")
    g = create_genesis(ident, {"hello": "world"}, "session-start")
    assert g.prev_hash == GENESIS_PREV_HASH
    assert g.agent_id == "renee_persona"
    assert g.action == "session-start"
    assert verify_attestation(ident, g) is True


def test_append_chains_of_various_lengths():
    ident = create_identity("renee_persona")
    for n in (2, 5, 100):
        chain = _build_chain(ident, n)
        assert len(chain) == n
        assert verify_chain(chain, ident) is True


def test_verify_chain_clean():
    ident = create_identity("renee_persona")
    chain = _build_chain(ident, 10)
    assert verify_chain(chain, ident) is True
    assert find_tamper(chain, ident) is None


def test_find_tamper_on_action_descriptor():
    ident = create_identity("renee_persona")
    chain = _build_chain(ident, 10)
    # Mutate action at index 3 without resigning -> signature won't match.
    chain[3] = Attestation(**{**chain[3].__dict__, "action": "tampered-action"})
    assert find_tamper(chain, ident) == 3


def test_find_tamper_on_state_hash():
    ident = create_identity("renee_persona")
    chain = _build_chain(ident, 10)
    chain[7] = Attestation(
        **{**chain[7].__dict__, "state_hash": "0" * 64}
    )
    assert find_tamper(chain, ident) == 7


def test_find_tamper_on_swap_in_middle():
    ident = create_identity("renee_persona")
    chain = _build_chain(ident, 10)
    chain[3], chain[4] = chain[4], chain[3]
    # After swap, chain[3].prev_hash refers to what was the old chain[3], not
    # the current chain[2], so the first index where the hash-link breaks is 3.
    idx = find_tamper(chain, ident)
    assert idx == 3


def test_cross_agent_forgery_rejected():
    alice = create_identity("alice")
    bob = create_identity("bob")
    chain = _build_chain(alice, 3)
    # Verifying Alice's chain under Bob's identity must fail.
    assert verify_chain(chain, bob) is False
    assert find_tamper(chain, bob) == 0


def test_cross_chain_state_hash_collision_reported():
    """Same state_blob in two chains yields the same state_hash; the
    collision is surfaced but NOT treated as an error."""
    alice = create_identity("alice")
    bob = create_identity("bob")
    shared_state = {"world-snapshot": "2026-04-20T12:00:00Z"}
    chain_a = [
        create_genesis(alice, shared_state, "start"),
        append(None if False else _build_chain(alice, 1)[0], alice, {"x": 1}, "step"),
    ]
    # Rebuild chain_a properly (the placeholder above is just to satisfy the
    # linter; genesis is the real first element, then we append a second).
    chain_a = [create_genesis(alice, shared_state, "start")]
    chain_a.append(append(chain_a[-1], alice, {"x": 1}, "step"))

    chain_b = [create_genesis(bob, {"unrelated": True}, "start")]
    chain_b.append(append(chain_b[-1], bob, shared_state, "absorb-state"))

    report = cross_chain_collision_report(chain_a, chain_b)
    assert len(report) == 1
    assert report[0]["idx_in_a"] == 0
    assert report[0]["idx_in_b"] == 1
    # Neither chain should be considered tampered by the collision alone.
    assert verify_chain(chain_a, alice) is True
    assert verify_chain(chain_b, bob) is True


def test_serialize_roundtrip(tmp_path: Path):
    ident = create_identity("renee_persona")
    chain = _build_chain(ident, 5)
    p = tmp_path / "chain.jsonl"
    serialize_chain(chain, p)
    loaded = load_chain(p)
    assert len(loaded) == len(chain)
    for a, b in zip(loaded, chain):
        assert a.to_dict() == b.to_dict()
    assert verify_chain(loaded, ident) is True


def test_empty_chain_is_vacuously_valid():
    assert verify_chain([]) is True
    # With an identity that's never consulted, still vacuous.
    ident = create_identity("anyone")
    assert verify_chain([], ident) is True


def test_single_attestation_chain_validates():
    ident = create_identity("renee_persona")
    g = create_genesis(ident, {"solo": True}, "only-step")
    assert verify_chain([g], ident) is True


def test_load_chain_on_corrupt_file_raises_clear_error(tmp_path: Path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        '{"agent_id": "a", "action": "x", "timestamp": "t", "state_hash": "sh", "prev_hash": "ph", "signature": "sig"}\n'
        "{not valid json at all}\n",
        encoding="utf-8",
    )
    with pytest.raises(ChainLoadError) as exc_info:
        load_chain(bad)
    assert "line 2" in str(exc_info.value)


def test_load_chain_on_missing_field_raises_clear_error(tmp_path: Path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"agent_id": "a", "action": "x"}\n', encoding="utf-8")
    with pytest.raises(ChainLoadError) as exc_info:
        load_chain(bad)
    assert "missing field" in str(exc_info.value)


def test_load_chain_on_missing_file_raises_clear_error(tmp_path: Path):
    with pytest.raises(ChainLoadError) as exc_info:
        load_chain(tmp_path / "does-not-exist.jsonl")
    assert "not found" in str(exc_info.value)


def test_hash_attestation_detects_single_byte_mutation():
    ident = create_identity("renee_persona")
    g = create_genesis(ident, {"a": 1}, "step")
    h1 = hash_attestation(g)
    g2 = Attestation(**{**g.__dict__, "action": "different"})
    h2 = hash_attestation(g2)
    assert h1 != h2
