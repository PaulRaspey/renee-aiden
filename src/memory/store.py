"""
Memory store: SQLite + FAISS index, emotionally-weighted retrieval with tiers.

Schema:
  content, embedding (blob), emotional_valence, emotional_intensity, salience,
  tier, created_at, last_referenced, reference_count, source_turn_id, tags.

Retrieval weights:
  score = (semantic*0.4 + emotional*0.2 + recency*0.2) * tier_weight + trigger_bonus

Tiers: ephemeral, casual, significant, inside_joke, core, sensitive.

Write path is minimal-LLM: use a small local model call (Ollama Gemma) to
extract 0-3 candidate memories from the turn, classify tier and valence.
If Ollama is unavailable, fall back to a pure-heuristic extractor.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime
from enum import Enum
from pathlib import Path

import numpy as np

try:
    import faiss  # type: ignore
except ImportError:  # pragma: no cover
    faiss = None  # type: ignore

try:
    from sentence_transformers import SentenceTransformer
except ImportError:  # pragma: no cover
    SentenceTransformer = None  # type: ignore


class MemoryTier(str, Enum):
    EPHEMERAL = "ephemeral"
    CASUAL = "casual"
    SIGNIFICANT = "significant"
    INSIDE_JOKE = "inside_joke"
    CORE = "core"
    SENSITIVE = "sensitive"


TIER_WEIGHTS = {
    MemoryTier.EPHEMERAL: 0.3,
    MemoryTier.CASUAL: 0.6,
    MemoryTier.SIGNIFICANT: 1.2,
    MemoryTier.INSIDE_JOKE: 1.5,
    MemoryTier.CORE: 1.8,
    MemoryTier.SENSITIVE: 0.0,  # surfaced only when user raises it
}


@dataclass
class Memory:
    id: str
    content: str
    embedding: np.ndarray
    emotional_valence: float
    emotional_intensity: float
    salience: float
    tier: MemoryTier
    created_at: float
    last_referenced: float
    reference_count: int
    source_turn_id: str
    tags: list[str] = field(default_factory=list)
    contextual_triggers: list[str] = field(default_factory=list)

    def to_row(self) -> tuple:
        return (
            self.id,
            self.content,
            self.embedding.astype("float32").tobytes(),
            self.emotional_valence,
            self.emotional_intensity,
            self.salience,
            self.tier.value,
            self.created_at,
            self.last_referenced,
            self.reference_count,
            self.source_turn_id,
            json.dumps(self.tags),
            json.dumps(self.contextual_triggers),
        )

    @classmethod
    def from_row(cls, row: tuple, dim: int) -> "Memory":
        (
            mid, content, emb_blob, val, inten, sal, tier, created, last_ref,
            ref_count, src, tags_json, triggers_json,
        ) = row
        emb = np.frombuffer(emb_blob, dtype="float32").copy()
        if emb.size != dim:
            emb = np.resize(emb, dim)
        return cls(
            id=mid, content=content, embedding=emb,
            emotional_valence=val, emotional_intensity=inten, salience=sal,
            tier=MemoryTier(tier),
            created_at=created, last_referenced=last_ref, reference_count=ref_count,
            source_turn_id=src,
            tags=json.loads(tags_json or "[]"),
            contextual_triggers=json.loads(triggers_json or "[]"),
        )


class EmbeddingBackend:
    """Wraps sentence-transformers. Lazily loads model on first use."""

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2"):
        self.model_name = model_name
        self._model = None
        self._dim = 384

    @property
    def dim(self) -> int:
        return self._dim

    def _ensure_loaded(self):
        if self._model is not None:
            return
        if SentenceTransformer is None:
            raise RuntimeError("sentence-transformers not installed")
        self._model = SentenceTransformer(self.model_name)
        self._dim = self._model.get_sentence_embedding_dimension() or 384

    def embed(self, text: str) -> np.ndarray:
        self._ensure_loaded()
        vec = self._model.encode([text], normalize_embeddings=True)[0]
        return np.asarray(vec, dtype="float32")

    def embed_many(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim), dtype="float32")
        self._ensure_loaded()
        vecs = self._model.encode(texts, normalize_embeddings=True)
        return np.asarray(vecs, dtype="float32")


class MemoryStore:
    """SQLite + FAISS emotionally-weighted memory for one persona."""

    def __init__(
        self,
        persona_name: str,
        state_dir: Path,
        extractor=None,
        embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2",
        core_facts: list[str] | None = None,
    ):
        self.persona_name = persona_name.lower()
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / f"{self.persona_name}_memory.db"
        self.embedding = EmbeddingBackend(embedding_model)
        self._index = None
        self._id_to_pos: dict[str, int] = {}
        self._pos_to_id: list[str] = []
        self._init_db()
        self.extractor = extractor  # lazy: MemoryExtractor
        # seed core facts once
        if core_facts:
            self._seed_core_facts(core_facts)

    def _init_db(self):
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    emotional_valence REAL,
                    emotional_intensity REAL,
                    salience REAL,
                    tier TEXT,
                    created_at REAL,
                    last_referenced REAL,
                    reference_count INTEGER DEFAULT 0,
                    source_turn_id TEXT,
                    tags TEXT,
                    contextual_triggers TEXT
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_tier ON memories(tier)"
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY,
                    ts REAL,
                    user_text TEXT,
                    assistant_text TEXT,
                    mood_json TEXT
                )
                """
            )

    def _seed_core_facts(self, core_facts: list[str]):
        with sqlite3.connect(self.db_path) as con:
            existing = {
                row[0]
                for row in con.execute(
                    "SELECT content FROM memories WHERE tier = ?", (MemoryTier.CORE.value,)
                )
            }
        for fact in core_facts:
            if fact in existing:
                continue
            self._insert(
                Memory(
                    id=f"core-{uuid.uuid4().hex[:10]}",
                    content=fact,
                    embedding=self.embedding.embed(fact),
                    emotional_valence=0.2,
                    emotional_intensity=0.5,
                    salience=1.0,
                    tier=MemoryTier.CORE,
                    created_at=time.time(),
                    last_referenced=time.time(),
                    reference_count=0,
                    source_turn_id="seed",
                    tags=["core", "identity"],
                    contextual_triggers=[],
                )
            )

    def _build_index(self):
        if faiss is None:
            self._index = None
            return
        with sqlite3.connect(self.db_path) as con:
            rows = list(con.execute(
                "SELECT id, embedding FROM memories WHERE tier != ?",
                (MemoryTier.SENSITIVE.value,),
            ))
        self._id_to_pos = {}
        self._pos_to_id = []
        if not rows:
            self._index = faiss.IndexFlatIP(self.embedding.dim)
            return
        mat = np.stack([np.frombuffer(b, dtype="float32") for _, b in rows]).astype("float32")
        # in case dim mismatch from older runs
        if mat.shape[1] != self.embedding.dim:
            mat = np.resize(mat, (mat.shape[0], self.embedding.dim))
        faiss.normalize_L2(mat)
        self._index = faiss.IndexFlatIP(self.embedding.dim)
        self._index.add(mat)
        for i, (mid, _) in enumerate(rows):
            self._id_to_pos[mid] = i
            self._pos_to_id.append(mid)

    # ------------------------------------------------------------------
    def _insert(self, mem: Memory):
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                INSERT OR REPLACE INTO memories
                (id,content,embedding,emotional_valence,emotional_intensity,salience,
                 tier,created_at,last_referenced,reference_count,source_turn_id,tags,contextual_triggers)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                mem.to_row(),
            )
        # invalidate index
        self._index = None

    def add_memory(
        self,
        content: str,
        tier: MemoryTier = MemoryTier.CASUAL,
        emotional_valence: float = 0.0,
        emotional_intensity: float = 0.3,
        salience: float = 0.5,
        tags: list[str] | None = None,
        triggers: list[str] | None = None,
        source_turn_id: str = "manual",
    ) -> Memory:
        mem = Memory(
            id=f"mem-{uuid.uuid4().hex[:12]}",
            content=content,
            embedding=self.embedding.embed(content),
            emotional_valence=emotional_valence,
            emotional_intensity=emotional_intensity,
            salience=salience,
            tier=tier,
            created_at=time.time(),
            last_referenced=time.time(),
            reference_count=0,
            source_turn_id=source_turn_id,
            tags=tags or [],
            contextual_triggers=triggers or [],
        )
        self._insert(mem)
        return mem

    # ------------------------------------------------------------------
    def write_turn(self, user_text: str, assistant_text: str, mood) -> list[Memory]:
        """Log the turn and extract candidate memories via the extractor."""
        turn_id = f"turn-{uuid.uuid4().hex[:12]}"
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO turns (id,ts,user_text,assistant_text,mood_json) VALUES (?,?,?,?,?)",
                (turn_id, time.time(), user_text, assistant_text, json.dumps(getattr(mood, "__dict__", {}))),
            )
        extracted: list[Memory] = []
        if self.extractor is None:
            return extracted
        try:
            candidates = self.extractor.extract(user_text=user_text, assistant_text=assistant_text)
        except Exception:
            candidates = []
        for c in candidates:
            mem = Memory(
                id=f"mem-{uuid.uuid4().hex[:12]}",
                content=c["content"],
                embedding=self.embedding.embed(c["content"]),
                emotional_valence=float(c.get("emotional_valence", 0.0)),
                emotional_intensity=float(c.get("emotional_intensity", 0.3)),
                salience=float(c.get("salience", 0.5)),
                tier=MemoryTier(c.get("tier", "casual")),
                created_at=time.time(),
                last_referenced=time.time(),
                reference_count=0,
                source_turn_id=turn_id,
                tags=list(c.get("tags", [])),
                contextual_triggers=list(c.get("contextual_triggers", [])),
            )
            self._insert(mem)
            extracted.append(mem)
        return extracted

    # ------------------------------------------------------------------
    def retrieve(self, query: str, mood=None, k: int = 8, user_raised_sensitive: bool = False) -> list[dict]:
        q_emb = self.embedding.embed(query)
        # load everything (small-scale personal memory, fine)
        with sqlite3.connect(self.db_path) as con:
            rows = list(con.execute(
                "SELECT id,content,embedding,emotional_valence,emotional_intensity,salience,"
                "tier,created_at,last_referenced,reference_count,source_turn_id,tags,contextual_triggers "
                "FROM memories"
            ))
        if not rows:
            return []
        now = time.time()
        query_valence = 0.0
        if mood is not None:
            query_valence = (getattr(mood, "warmth", 0.5) - 0.5) * 2.0

        scored: list[tuple[float, Memory]] = []
        for row in rows:
            m = Memory.from_row(row, dim=self.embedding.dim)
            # Sensitive memories are completely hidden unless the user raised the topic.
            if m.tier is MemoryTier.SENSITIVE and not user_raised_sensitive:
                continue
            semantic = float(np.dot(q_emb, m.embedding))
            emotional = 1.0 - abs(m.emotional_valence - query_valence) / 2.0
            age_days = max(0.0, (now - m.created_at) / 86400.0)
            last_ref_days = max(0.0, (now - m.last_referenced) / 86400.0)
            recency = math.exp(-age_days / 30.0) + math.exp(-last_ref_days / 7.0)
            tier_w = TIER_WEIGHTS[m.tier]
            if m.tier is MemoryTier.SENSITIVE:
                tier_w = 2.0
            trigger_bonus = 0.0
            if m.contextual_triggers:
                q_low = query.lower()
                trigger_bonus = 0.3 * sum(1 for t in m.contextual_triggers if t.lower() in q_low)
            score = (semantic * 0.5 + emotional * 0.2 + recency * 0.2) * tier_w + trigger_bonus + 0.05 * m.salience
            scored.append((score, m))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:k]
        # touch last_referenced for the ones we surface
        with sqlite3.connect(self.db_path) as con:
            for score, m in top:
                con.execute(
                    "UPDATE memories SET last_referenced=?, reference_count=reference_count+1 WHERE id=?",
                    (now, m.id),
                )
        return [
            {
                "id": m.id,
                "content": m.content,
                "tier": m.tier.value,
                "emotional_valence": m.emotional_valence,
                "score": float(score),
            }
            for score, m in top
        ]

    # ------------------------------------------------------------------
    def recent_turns(self, n: int = 20) -> list[dict]:
        with sqlite3.connect(self.db_path) as con:
            rows = list(con.execute(
                "SELECT id, ts, user_text, assistant_text FROM turns ORDER BY ts DESC LIMIT ?",
                (n,),
            ))
        return [
            {"id": r[0], "ts": r[1], "user": r[2], "assistant": r[3]}
            for r in reversed(rows)
        ]

    def count(self) -> int:
        with sqlite3.connect(self.db_path) as con:
            return con.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
