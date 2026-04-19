"""UAHP identity: create, sign, verify, persist, and cross-verify receipts."""
from pathlib import Path

import pytest

from src.identity import (
    AgentIdentity,
    ReneeIdentityManager,
    load_or_create,
    sign_receipt,
    verify_receipt,
)
from src.identity.uahp_identity import create_identity


def test_sign_and_verify_roundtrip():
    identity = create_identity("test-agent")
    msg = "hello world"
    sig = identity.sign(msg)
    assert identity.verify(msg, sig)
    assert not identity.verify("tampered", sig)


def test_receipt_roundtrip():
    identity = create_identity("renee_persona")
    receipt = sign_receipt(
        identity,
        task_id="t1",
        action="persona.respond",
        duration_ms=123.4,
        success=True,
        input_data={"user": "hi"},
        output_data={"text": "hey"},
    )
    assert verify_receipt(identity, receipt)
    # tampering breaks verification
    receipt.duration_ms = 9999
    assert not verify_receipt(identity, receipt)


def test_load_or_create_persists(tmp_path: Path):
    id1 = load_or_create("renee_persona", tmp_path)
    id2 = load_or_create("renee_persona", tmp_path)
    assert id1.signing_key == id2.signing_key
    assert id1.public_hash == id2.public_hash


def test_bootstrap_all(tmp_path: Path):
    mgr = ReneeIdentityManager(tmp_path)
    ids = mgr.bootstrap_all()
    assert "renee_persona" in ids
    assert "aiden_persona" in ids
    # all distinct keys
    keys = {i.signing_key for i in ids.values()}
    assert len(keys) == len(ids)


# --------------------------- hardening ---------------------------------


def test_verify_rejects_signature_from_different_identity():
    """A receipt signed by agent A must not verify under agent B's
    identity, even if the rest of the receipt fields are identical."""
    a = create_identity("agent-a")
    b = create_identity("agent-b")
    receipt = sign_receipt(
        a, task_id="t", action="act", duration_ms=1.0, success=True,
        input_data={"x": 1}, output_data={"y": 2},
    )
    assert verify_receipt(a, receipt)
    assert not verify_receipt(b, receipt), "cross-identity verify must fail"


def test_verify_rejects_input_hash_swap():
    """Replacing just the input_hash breaks the signature."""
    identity = create_identity("tamper-test")
    receipt = sign_receipt(
        identity, task_id="t", action="act", duration_ms=1.0, success=True,
        input_data={"x": 1}, output_data={"y": 2},
    )
    receipt.input_hash = "0" * 64
    assert not verify_receipt(identity, receipt)


def test_each_receipt_has_unique_id_and_timestamp():
    """Two signatures on the same content must have distinct receipt_ids
    and timestamps. Without this, a replay-detector keyed on receipt_id
    cannot distinguish duplicates."""
    import time as _t

    identity = create_identity("id-test")
    r1 = sign_receipt(
        identity, task_id="t", action="act", duration_ms=1.0, success=True,
        input_data={"x": 1}, output_data={"y": 2},
    )
    _t.sleep(0.002)
    r2 = sign_receipt(
        identity, task_id="t", action="act", duration_ms=1.0, success=True,
        input_data={"x": 1}, output_data={"y": 2},
    )
    assert r1.receipt_id != r2.receipt_id
    assert r2.timestamp >= r1.timestamp
    # Sanity: both verify, and they aren't the same signed object.
    assert verify_receipt(identity, r1) and verify_receipt(identity, r2)
    assert r1.signature != r2.signature
