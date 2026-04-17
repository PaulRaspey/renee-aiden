from pathlib import Path

import pytest

from src.memory import MemoryStore, MemoryTier


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    s = MemoryStore(persona_name="test", state_dir=tmp_path)
    return s


def test_write_and_retrieve_semantic(store: MemoryStore):
    store.add_memory("PJ loves preserved lemon on pizza.", tier=MemoryTier.CASUAL, tags=["food"])
    store.add_memory("PJ is working on UAHP protocol stack.", tier=MemoryTier.CORE, tags=["work"])
    store.add_memory("PJ teaches at Pioneer Tech.", tier=MemoryTier.CORE, tags=["teaching"])

    hits = store.retrieve("what is PJ working on right now", k=3)
    assert hits
    assert any("UAHP" in h["content"] or "Pioneer Tech" in h["content"] for h in hits)


def test_tier_weighting_boosts_core(store: MemoryStore):
    store.add_memory("We had fries yesterday.", tier=MemoryTier.EPHEMERAL, tags=["food"])
    store.add_memory("PJ's partner matters enormously to him.", tier=MemoryTier.CORE, tags=["family"])
    hits = store.retrieve("who does PJ care about", k=2)
    top = hits[0]
    assert top["tier"] in (MemoryTier.CORE.value, MemoryTier.SIGNIFICANT.value)


def test_sensitive_suppressed_by_default(store: MemoryStore):
    store.add_memory(
        "PJ has been grieving a lot this year.",
        tier=MemoryTier.SENSITIVE,
        emotional_valence=-0.7,
        emotional_intensity=0.8,
        tags=["grief"],
    )
    store.add_memory(
        "PJ likes tie-dye.",
        tier=MemoryTier.CASUAL,
        tags=["hobby"],
    )
    hits = store.retrieve("tell me something you know", k=5, user_raised_sensitive=False)
    for h in hits:
        assert h["tier"] != MemoryTier.SENSITIVE.value


def test_reference_count_updates(store: MemoryStore):
    store.add_memory("PJ loves Phoebe Bridgers.", tier=MemoryTier.SIGNIFICANT, tags=["music"])
    store.retrieve("what music does PJ like", k=3)
    store.retrieve("what music does PJ like", k=3)
    import sqlite3
    with sqlite3.connect(store.db_path) as con:
        row = con.execute("SELECT reference_count FROM memories WHERE content LIKE '%Phoebe%'").fetchone()
    assert row[0] >= 2
