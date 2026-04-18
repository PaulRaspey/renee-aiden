"""Regression tests for scripts/generate_paralinguistic_library.py.

Covers the invariant from Decision #29: `--only` must never drop entries
from the index. Specifically, when `--count N` is passed for a selected
category with >N existing WAVs on disk, all WAVs must still be indexed.
Previously `generate_category` truncated at `count`, silently losing
the extras.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "generate_paralinguistic_library.py"


def _write_fake_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(1000, dtype=np.float32), 22050)


def test_only_with_count_below_existing_preserves_full_index(tmp_path: Path):
    """10 WAVs on disk + --count 5 --only laughs/hearty → metadata still has all 10."""
    base_dir = tmp_path / "paralinguistics" / "renee"
    hearty_dir = base_dir / "laughs" / "hearty"
    for i in range(1, 11):
        _write_fake_wav(hearty_dir / f"hearty_{i:03d}.wav")

    env = os.environ.copy()
    env.setdefault("RENEE_VOICE_ID", "dummy_voice_id")
    env.setdefault("ELEVENLABS_API_KEY", "dummy_key_not_used_in_dry_run")

    result = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--count", "5",
            "--only", "laughs/hearty",
            "--dry-run",
            "--base-dir", str(base_dir),
        ],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, f"script failed: {result.stderr}"

    meta = yaml.safe_load((base_dir / "metadata.yaml").read_text(encoding="utf-8"))
    hearty = [c for c in meta["clips"] if c.get("subcategory") == "hearty"]
    assert len(hearty) == 10, (
        f"index dropped entries below existing-on-disk count: "
        f"expected 10, got {len(hearty)}"
    )
    # Every on-disk file must appear in the index.
    indexed_files = {c["file"] for c in hearty}
    expected = {f"laughs/hearty/hearty_{i:03d}.wav" for i in range(1, 11)}
    assert indexed_files == expected
