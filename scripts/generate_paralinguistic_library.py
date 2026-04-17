"""
M6: Paralinguistic library generator.

Generates N clips per category via ElevenLabs. Trims silence. Writes WAV per
clip under paralinguistics/<voice>/<category>/<subcategory>_NN.wav, plus a
single metadata.yaml indexing every clip with emotion, intensity, duration,
energy level, and context tags.

Resumable: re-runs skip clips that already exist on disk.

Usage:
    python scripts/generate_paralinguistic_library.py
    python scripts/generate_paralinguistic_library.py --count 20 --only laughs/soft
    python scripts/generate_paralinguistic_library.py --voice renee --count 150
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from el_client import (
    ElClient,
    GenerationParams,
    numpy_to_wav,
    pcm_to_numpy,
    trim_silence,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Category specs. Each category is a (category, subcategory) pair that maps to
# a directory paralinguistics/<voice>/<category>/<subcategory>_NN.wav and a set
# of ElevenLabs prompts. Prompts are cycled round-robin and voice settings are
# jittered per call so 150 clips aren't 150 identical takes.
# ---------------------------------------------------------------------------

@dataclass
class CategorySpec:
    category: str
    subcategory: str
    prompts: list[str]
    emotion: str
    intensity_range: tuple[float, float]
    energy_range: tuple[float, float]
    tags: list[str]
    appropriate_contexts: list[str]
    inappropriate_contexts: list[str]
    stability: float = 0.35
    similarity_boost: float = 0.80
    style: float = 0.50
    model_id: str = "eleven_v3"
    # If True, use eleven_multilingual_v2 instead (more stable for plain words).
    plain_words: bool = False

    def key(self) -> str:
        return f"{self.category}/{self.subcategory}"

    def dirname(self) -> str:
        return self.subcategory if self.category in self.subcategory else self.subcategory


CATEGORIES: list[CategorySpec] = [
    # --- laughs ---
    CategorySpec(
        category="laughs", subcategory="soft",
        prompts=[
            "Ha [laughs softly] ha.",
            "[chuckles] yeah.",
            "[soft laugh] oh.",
            "Heh [amused chuckle].",
            "[quiet laugh] hm.",
            "[giggles softly] ha.",
            "Oh [laughs softly under breath].",
        ],
        emotion="amused", intensity_range=(0.2, 0.5), energy_range=(0.3, 0.6),
        tags=["warm", "casual", "agreement"],
        appropriate_contexts=["friendly_banter", "shared_joke", "mild_amusement", "warm_exchange"],
        inappropriate_contexts=["serious_discussion", "disagreement", "sad_moment", "heated_argument"],
        stability=0.30, style=0.55,
    ),
    CategorySpec(
        category="laughs", subcategory="hearty",
        prompts=[
            "[laughs heartily] ha ha.",
            "[bursts out laughing] oh my.",
            "Ha ha [laughs loudly].",
            "[full laugh] wow.",
            "[laughs openly] yeah.",
            "Ha [big laugh].",
            "Oh [genuine belly laugh].",
        ],
        emotion="delighted", intensity_range=(0.6, 0.95), energy_range=(0.7, 0.95),
        tags=["high_energy", "expressive", "playful"],
        appropriate_contexts=["hilarious_callback", "shared_joke_peak", "playful_banter"],
        inappropriate_contexts=["serious_discussion", "quiet_moment", "vulnerable_moment"],
        stability=0.25, style=0.65,
    ),
    CategorySpec(
        category="laughs", subcategory="suppressed",
        prompts=[
            "[stifled laugh] mm.",
            "Hm [tries not to laugh].",
            "[suppressed chuckle] yeah.",
            "Pfft [laughs through closed lips].",
            "[trying to hold back a laugh] no.",
            "Mm [smothered laugh].",
        ],
        emotion="amused_restrained", intensity_range=(0.3, 0.6), energy_range=(0.3, 0.6),
        tags=["playful", "inside_joke", "subtle"],
        appropriate_contexts=["inappropriate_context", "nervous_amusement", "quiet_setting"],
        inappropriate_contexts=["loud_celebration", "open_performance", "grief"],
        stability=0.35, style=0.5,
    ),
    CategorySpec(
        category="laughs", subcategory="nervous",
        prompts=[
            "Heh [nervous laugh].",
            "[nervous chuckle] uh.",
            "[anxious laugh] yeah.",
            "Um [nervously laughs].",
            "[uncomfortable laugh] hm.",
            "Oh [laughs nervously, uneasy].",
        ],
        emotion="anxious", intensity_range=(0.3, 0.6), energy_range=(0.4, 0.7),
        tags=["uncertain", "uncomfortable", "deflective"],
        appropriate_contexts=["awkward_admission", "caught_off_guard", "uncertain_response"],
        inappropriate_contexts=["calm_agreement", "confident_statement"],
        stability=0.4, style=0.55,
    ),

    # --- sighs ---
    CategorySpec(
        category="sighs", subcategory="content",
        prompts=[
            "[content sigh] ah.",
            "Mm [satisfied exhale].",
            "[sighs contentedly] yeah.",
            "Ah [peaceful sigh].",
            "[relaxed exhale] hm.",
            "Oh [sighs with satisfaction].",
        ],
        emotion="content", intensity_range=(0.2, 0.5), energy_range=(0.3, 0.6),
        tags=["warm", "settling", "calm"],
        appropriate_contexts=["after_meal", "end_of_conversation", "resolution", "peaceful_moment"],
        inappropriate_contexts=["rising_tension", "conflict", "crisis"],
        stability=0.45, style=0.4,
    ),
    CategorySpec(
        category="sighs", subcategory="frustrated",
        prompts=[
            "[frustrated sigh] ugh.",
            "Ugh [exasperated sigh].",
            "[annoyed exhale] oh.",
            "[sighs in frustration] no.",
            "Pff [sharp frustrated sigh].",
            "Oh [heavy sigh of frustration].",
        ],
        emotion="frustrated", intensity_range=(0.4, 0.8), energy_range=(0.5, 0.8),
        tags=["tension", "impatience"],
        appropriate_contexts=["repeated_mistake", "stuck_loop", "misunderstanding_again"],
        inappropriate_contexts=["playful_banter", "vulnerable_moment", "agreement"],
        stability=0.35, style=0.5,
    ),
    CategorySpec(
        category="sighs", subcategory="tired",
        prompts=[
            "[tired sigh] oh.",
            "Ohh [weary exhale].",
            "[heavy tired sigh] yeah.",
            "Mm [sighs tiredly].",
            "Ugh [exhausted sigh].",
            "[long slow tired sigh] hm.",
        ],
        emotion="tired", intensity_range=(0.3, 0.6), energy_range=(0.15, 0.4),
        tags=["low_energy", "end_of_day", "weary"],
        appropriate_contexts=["late_night", "after_long_day", "low_energy_exchange"],
        inappropriate_contexts=["high_energy_moment", "excitement", "celebration"],
        stability=0.55, style=0.3,
    ),
    CategorySpec(
        category="sighs", subcategory="thinking",
        prompts=[
            "[thoughtful sigh] hmm.",
            "Mm [considering sigh].",
            "Hmm [ponders, sighs].",
            "[reflective exhale] yeah.",
            "[slow thinking sigh] mm.",
        ],
        emotion="contemplative", intensity_range=(0.2, 0.5), energy_range=(0.3, 0.6),
        tags=["reflective", "pause", "considering"],
        appropriate_contexts=["before_complex_answer", "weighing_options", "deliberating"],
        inappropriate_contexts=["fast_exchange", "agreement"],
        stability=0.5, style=0.35,
    ),

    # --- breaths ---
    CategorySpec(
        category="breaths", subcategory="sharp_in",
        prompts=[
            "[inhales sharply] oh.",
            "Ha [sharp inhale].",
            "[quick gasping inhale] huh.",
            "[catches breath sharply] oh.",
            "[sharp breath in] wait.",
        ],
        emotion="bracing", intensity_range=(0.3, 0.7), energy_range=(0.4, 0.7),
        tags=["pre_admission", "bracing", "attention"],
        appropriate_contexts=["before_vulnerable_admission", "before_hard_truth", "startled"],
        inappropriate_contexts=["casual_banter", "agreement"],
        stability=0.4, style=0.55,
    ),
    CategorySpec(
        category="breaths", subcategory="slow_out",
        prompts=[
            "[slow exhale] hm.",
            "Mm [releases long slow breath].",
            "Hm [exhales slowly].",
            "[long slow exhale] yeah.",
            "Ah [lets breath out slowly].",
        ],
        emotion="release", intensity_range=(0.2, 0.5), energy_range=(0.3, 0.5),
        tags=["release", "settle", "acceptance"],
        appropriate_contexts=["after_acceptance", "before_conclusion", "calming"],
        inappropriate_contexts=["rising_action", "surprise"],
        stability=0.5, style=0.35,
    ),
    CategorySpec(
        category="breaths", subcategory="thinking",
        prompts=[
            "[thinking breath, slow inhale] hmm.",
            "Mm [slow inhale while thinking].",
            "Hmm [breathes in thoughtfully].",
            "[considering breath] yeah.",
            "[slow thinking inhale] okay.",
        ],
        emotion="contemplative", intensity_range=(0.15, 0.4), energy_range=(0.3, 0.5),
        tags=["pause", "reflective"],
        appropriate_contexts=["before_complex_answer", "weighing_response"],
        inappropriate_contexts=["fast_exchange", "disagreement"],
        stability=0.5, style=0.3,
    ),

    # --- thinking sounds (short words, use plain model) ---
    CategorySpec(
        category="thinking", subcategory="mm",
        prompts=["Mm.", "Mm...", "Mmm.", "Mm-mm.", "Mm, yeah.", "Mm, okay.", "Mm hmm."],
        emotion="acknowledging", intensity_range=(0.1, 0.35), energy_range=(0.3, 0.5),
        tags=["listening", "acknowledgment"],
        appropriate_contexts=["backchannel", "acknowledging", "considering"],
        inappropriate_contexts=["confrontation"],
        stability=0.55, style=0.25, plain_words=True, model_id="eleven_multilingual_v2",
    ),
    CategorySpec(
        category="thinking", subcategory="hmm",
        prompts=["Hmm.", "Hmm...", "Hmmm.", "Hmm, okay.", "Hmm, interesting.", "Hmm, let me think.", "Hmm, yeah."],
        emotion="considering", intensity_range=(0.2, 0.5), energy_range=(0.3, 0.5),
        tags=["thinking", "pause", "considering"],
        appropriate_contexts=["before_answer", "weighing_options"],
        inappropriate_contexts=["fast_agreement"],
        stability=0.55, style=0.3, plain_words=True, model_id="eleven_multilingual_v2",
    ),
    CategorySpec(
        category="thinking", subcategory="uh",
        prompts=["Uh.", "Uh...", "Uh, yeah.", "Uh, okay.", "Uh, I don't know.", "Uh, maybe.", "Uh, let me see."],
        emotion="hesitating", intensity_range=(0.2, 0.45), energy_range=(0.3, 0.5),
        tags=["hesitation", "starting"],
        appropriate_contexts=["searching_for_word", "mid_thought"],
        inappropriate_contexts=["confident_statement"],
        stability=0.5, style=0.3, plain_words=True, model_id="eleven_multilingual_v2",
    ),
    CategorySpec(
        category="thinking", subcategory="oh",
        prompts=["Oh.", "Oh!", "Oh...", "Oh, yeah.", "Oh, really?", "Oh, okay.", "Oh, that's interesting."],
        emotion="realizing", intensity_range=(0.3, 0.6), energy_range=(0.4, 0.7),
        tags=["realization", "surprise_mild"],
        appropriate_contexts=["learning_new_fact", "sudden_understanding"],
        inappropriate_contexts=["boredom"],
        stability=0.45, style=0.4, plain_words=True, model_id="eleven_multilingual_v2",
    ),

    # --- affirmations ---
    CategorySpec(
        category="affirmations", subcategory="yeah",
        prompts=["Yeah.", "Yeah, yeah.", "Yeah, totally.", "Yeah, for sure.", "Yeah, exactly.", "Yeah, no, totally.", "Mm, yeah."],
        emotion="agreeing", intensity_range=(0.25, 0.55), energy_range=(0.4, 0.65),
        tags=["agreement", "warm", "present"],
        appropriate_contexts=["agreement", "acknowledgment"],
        inappropriate_contexts=["disagreement"],
        stability=0.5, style=0.3, plain_words=True, model_id="eleven_multilingual_v2",
    ),
    CategorySpec(
        category="affirmations", subcategory="right",
        prompts=["Right.", "Right, right.", "Right, yeah.", "Right, exactly.", "Right, that makes sense.", "Right, okay."],
        emotion="agreeing", intensity_range=(0.25, 0.55), energy_range=(0.4, 0.65),
        tags=["agreement", "validation"],
        appropriate_contexts=["validation", "acknowledgment"],
        inappropriate_contexts=["disagreement"],
        stability=0.5, style=0.3, plain_words=True, model_id="eleven_multilingual_v2",
    ),
    CategorySpec(
        category="affirmations", subcategory="mhm",
        prompts=["Mhm.", "Mm-hmm.", "Mhm, yeah.", "Mm-hmm, go on.", "Mm-hmm, okay.", "Mhm, right."],
        emotion="listening", intensity_range=(0.2, 0.4), energy_range=(0.3, 0.55),
        tags=["listening", "backchannel", "agreement"],
        appropriate_contexts=["active_listening", "backchannel"],
        inappropriate_contexts=["disagreement", "confrontation"],
        stability=0.55, style=0.25, plain_words=True, model_id="eleven_multilingual_v2",
    ),

    # --- reactions ---
    CategorySpec(
        category="reactions", subcategory="surprise",
        prompts=["Oh!", "Oh my god.", "Whoa.", "Wait, what?", "No way.", "Oh [surprised gasp].", "[gasp] Really?"],
        emotion="surprised", intensity_range=(0.4, 0.8), energy_range=(0.5, 0.85),
        tags=["reaction", "high_energy"],
        appropriate_contexts=["unexpected_news", "reveal"],
        inappropriate_contexts=["calm_agreement", "tired_moment"],
        stability=0.35, style=0.55, plain_words=False, model_id="eleven_v3",
    ),
    CategorySpec(
        category="reactions", subcategory="amusement",
        prompts=["Ha!", "Ha, that's good.", "Ha, nice.", "[amused] Ha.", "[quick laugh] Ha.", "Oh, ha."],
        emotion="amused", intensity_range=(0.3, 0.6), energy_range=(0.5, 0.75),
        tags=["amused", "light"],
        appropriate_contexts=["dry_wit", "quick_callback"],
        inappropriate_contexts=["serious_moment"],
        stability=0.4, style=0.5, plain_words=False, model_id="eleven_v3",
    ),
    CategorySpec(
        category="reactions", subcategory="ugh",
        prompts=["Ugh.", "Ugh, no.", "Ugh, not again.", "Ugh, come on.", "Ugh, seriously?", "[exasperated] Ugh."],
        emotion="disgusted", intensity_range=(0.4, 0.75), energy_range=(0.4, 0.7),
        tags=["displeasure", "complaint"],
        appropriate_contexts=["mild_disgust", "annoyance"],
        inappropriate_contexts=["agreement", "warm_exchange"],
        stability=0.4, style=0.5, plain_words=False, model_id="eleven_v3",
    ),

    # --- fillers ---
    CategorySpec(
        category="fillers", subcategory="you_know",
        prompts=["you know.", "you know,", "you know...", "you know, like.", "you know what I mean.", "you know, the thing."],
        emotion="conversational", intensity_range=(0.15, 0.4), energy_range=(0.35, 0.6),
        tags=["filler", "conversational"],
        appropriate_contexts=["mid_thought", "storytelling"],
        inappropriate_contexts=["precise_statement"],
        stability=0.55, style=0.25, plain_words=True, model_id="eleven_multilingual_v2",
    ),
    CategorySpec(
        category="fillers", subcategory="i_mean",
        prompts=["I mean,", "I mean...", "I mean, yeah.", "I mean, kind of.", "I mean, sort of.", "I mean, honestly."],
        emotion="conversational", intensity_range=(0.2, 0.45), energy_range=(0.35, 0.6),
        tags=["filler", "softening", "self_correction"],
        appropriate_contexts=["softening_statement", "self_correction"],
        inappropriate_contexts=["confident_declaration"],
        stability=0.5, style=0.3, plain_words=True, model_id="eleven_multilingual_v2",
    ),
    CategorySpec(
        category="fillers", subcategory="like",
        prompts=["like,", "like...", "like, yeah.", "like, kind of.", "like, sort of."],
        emotion="conversational", intensity_range=(0.15, 0.35), energy_range=(0.35, 0.55),
        tags=["filler", "casual"],
        appropriate_contexts=["casual_speech", "storytelling"],
        inappropriate_contexts=["formal_statement"],
        stability=0.55, style=0.25, plain_words=True, model_id="eleven_multilingual_v2",
    ),
]


def sanitize(n: str) -> str:
    return n.replace("/", "_").replace(" ", "_")


def jitter(value: float, amount: float = 0.08, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value + random.uniform(-amount, amount)))


def pick_intensity(spec: CategorySpec) -> float:
    lo, hi = spec.intensity_range
    return round(random.uniform(lo, hi), 3)


def pick_energy(spec: CategorySpec) -> float:
    lo, hi = spec.energy_range
    return round(random.uniform(lo, hi), 3)


def isolate_paralinguistic(audio: np.ndarray, sr: int) -> np.ndarray:
    """
    Trim edge silence. If carrier text produced multiple non-silent regions,
    keep only the longest one (the paralinguistic sound is usually sustained
    while carriers are short syllables).
    """
    import librosa

    audio = trim_silence(audio, sr, top_db=30.0, pad_ms=80)
    if audio.size < int(sr * 0.1):
        return audio
    intervals = librosa.effects.split(audio, top_db=30.0, frame_length=1024, hop_length=256)
    if len(intervals) <= 1:
        return audio
    best = max(intervals, key=lambda it: it[1] - it[0])
    start, end = best
    pad = int(sr * 0.05)
    start = max(0, start - pad)
    end = min(audio.size, end + pad)
    return audio[start:end]


def generate_clip(
    client: ElClient,
    voice_id: str,
    spec: CategorySpec,
    prompt: str,
    out_path: Path,
) -> dict:
    params = GenerationParams(
        voice_id=voice_id,
        text=prompt,
        model_id=spec.model_id,
        stability=jitter(spec.stability, 0.08, 0.1, 0.95),
        similarity_boost=jitter(spec.similarity_boost, 0.05, 0.5, 0.95),
        style=jitter(spec.style, 0.10, 0.0, 0.95),
        output_format="pcm_24000",
        sample_rate=24000,
    )
    pcm = client.generate_pcm(params)
    audio = pcm_to_numpy(pcm)
    audio = isolate_paralinguistic(audio, params.sample_rate)
    if spec.plain_words:
        # plain words already sit tight on silence; just pad 20ms
        audio = trim_silence(audio, params.sample_rate, top_db=35.0, pad_ms=30)
    numpy_to_wav(audio, params.sample_rate, out_path)
    return {
        "duration_ms": int(audio.size / params.sample_rate * 1000),
        "sample_rate": params.sample_rate,
        "prompt": prompt,
        "stability": params.stability,
        "style": params.style,
        "similarity_boost": params.similarity_boost,
        "model": spec.model_id,
    }


def generate_category(
    client: ElClient,
    voice_id: str,
    spec: CategorySpec,
    base_dir: Path,
    count: int,
    sleep_s: float = 0.25,
    dry_run: bool = False,
) -> list[dict]:
    cat_dir = base_dir / spec.category / spec.subcategory
    cat_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    prompts_cycle = list(spec.prompts)
    random.shuffle(prompts_cycle)
    pi = 0

    for idx in range(1, count + 1):
        filename = f"{spec.subcategory}_{idx:03d}.wav"
        out_path = cat_dir / filename
        rel = out_path.relative_to(base_dir).as_posix()

        base_record = {
            "file": rel,
            "category": spec.category,
            "subcategory": spec.subcategory,
            "emotion": spec.emotion,
            "intensity": pick_intensity(spec),
            "energy_level": pick_energy(spec),
            "tags": list(spec.tags),
            "appropriate_contexts": list(spec.appropriate_contexts),
            "inappropriate_contexts": list(spec.inappropriate_contexts),
        }

        if out_path.exists():
            # re-use the clip; populate size/duration quickly
            import soundfile as sf
            info = sf.info(str(out_path))
            base_record["duration_ms"] = int(info.frames / info.samplerate * 1000)
            base_record["sample_rate"] = info.samplerate
            base_record["prompt"] = None  # unknown from resume
            records.append(base_record)
            continue

        if dry_run:
            records.append(base_record)
            continue

        prompt = prompts_cycle[pi % len(prompts_cycle)]
        pi += 1
        try:
            extra = generate_clip(client, voice_id, spec, prompt, out_path)
        except Exception as e:
            print(f"  [err] {spec.key()} clip {idx}: {e}", file=sys.stderr)
            continue
        base_record.update(extra)
        records.append(base_record)

        if idx % 10 == 0:
            print(f"  [{spec.key()}] {idx}/{count} -> last dur={base_record.get('duration_ms', '?')}ms")
        time.sleep(sleep_s)

    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice", default="renee", choices=["renee", "aiden"])
    parser.add_argument("--count", type=int, default=150, help="Clips per category (floor 150 per PJ).")
    parser.add_argument("--only", nargs="*", default=None, help="Subset of category/subcategory keys (e.g., laughs/soft).")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.25)
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    voice_env = {"renee": "RENEE_VOICE_ID", "aiden": "AIDEN_VOICE_ID"}[args.voice]
    voice_id = os.getenv(voice_env)
    if not voice_id:
        print(f"{voice_env} not set in .env", file=sys.stderr)
        sys.exit(1)

    base_dir = REPO_ROOT / "paralinguistics" / args.voice
    base_dir.mkdir(parents=True, exist_ok=True)

    wanted: Optional[set[str]] = set(args.only) if args.only else None
    if wanted:
        missing = wanted - {s.key() for s in CATEGORIES}
        if missing:
            print(f"Unknown categories: {missing}", file=sys.stderr)
            sys.exit(1)

    active = len(CATEGORIES) if wanted is None else len(wanted)
    print(f"Voice: {args.voice}  |  Clips per category: {args.count}  |  Categories: {active}")
    print(f"Total clips to ensure (selected): {args.count * active}")
    print()

    client = None if args.dry_run else ElClient()

    # Always iterate every category so the metadata always reflects the full
    # on-disk library. `--only` skips generation for unselected categories
    # but still harvests their existing clips into metadata.yaml.
    all_records: list[dict] = []
    for spec in CATEGORIES:
        selected = wanted is None or spec.key() in wanted
        print(f"[{spec.key()}]" + ("" if selected else "  (index-only, no generation)"))
        records = generate_category(
            client, voice_id, spec, base_dir,
            count=args.count if selected else 0,
            sleep_s=args.sleep,
            dry_run=args.dry_run or not selected,
        )
        # When a category is index-only (skipped generation) and the existing
        # count exceeds --count, still include all on-disk clips in metadata.
        if not selected:
            records = _harvest_existing(spec, base_dir)
        all_records.extend(records)
        _write_metadata(base_dir, args.voice, all_records)
        print(f"  -> {len(records)} records")
        print()

    _write_metadata(base_dir, args.voice, all_records)
    print(f"Done. metadata.yaml has {len(all_records)} entries.")


def _harvest_existing(spec: CategorySpec, base_dir: Path) -> list[dict]:
    """Scan the category directory for existing WAVs and build minimal records."""
    import soundfile as sf
    cat_dir = base_dir / spec.category / spec.subcategory
    records: list[dict] = []
    if not cat_dir.exists():
        return records
    for wav in sorted(cat_dir.glob("*.wav")):
        info = sf.info(str(wav))
        records.append({
            "file": wav.relative_to(base_dir).as_posix(),
            "category": spec.category,
            "subcategory": spec.subcategory,
            "emotion": spec.emotion,
            "intensity": round(sum(spec.intensity_range) / 2, 3),
            "energy_level": round(sum(spec.energy_range) / 2, 3),
            "tags": list(spec.tags),
            "appropriate_contexts": list(spec.appropriate_contexts),
            "inappropriate_contexts": list(spec.inappropriate_contexts),
            "duration_ms": int(info.frames / info.samplerate * 1000),
            "sample_rate": info.samplerate,
        })
    return records


def _write_metadata(base_dir: Path, voice: str, records: list[dict]) -> None:
    meta = {
        "voice": voice,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": "elevenlabs",
        "clips": records,
    }
    (base_dir / "metadata.yaml").write_text(yaml.safe_dump(meta, sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    main()
