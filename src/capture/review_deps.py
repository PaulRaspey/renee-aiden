"""Review dependency detection and reporting.

Backs scripts/install_review_deps.bat. The .bat is a thin shell over
these functions so the idempotency and warning logic stays unit-testable
without needing to exercise pip or shell out.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
from dataclasses import dataclass


@dataclass
class DepSpec:
    pkg: str
    import_name: str
    size_mb: int
    desc: str


REVIEW_DEPS: list[DepSpec] = [
    DepSpec("whisperx", "whisperx", 250, "WhisperX transcription"),
    DepSpec("praat-parselmouth", "parselmouth", 30, "Parselmouth prosody"),
    DepSpec("pyannote.audio", "pyannote.audio", 400, "pyannote diarization"),
    DepSpec("matplotlib", "matplotlib", 30, "matplotlib plotting"),
    DepSpec("plotly", "plotly", 25, "plotly plotting"),
]


WHISPER_MODEL_SIZES_MB: dict[str, int] = {
    "tiny.en": 40,
    "base.en": 150,
    "small.en": 500,
    "medium.en": 1500,
    "large-v3": 3000,
}


@dataclass
class DepStatus:
    spec: DepSpec
    installed: bool


def check_installed(import_name: str) -> bool:
    try:
        spec = importlib.util.find_spec(import_name)
    except (ImportError, ValueError):
        return False
    return spec is not None


def status_all() -> list[DepStatus]:
    return [DepStatus(spec=d, installed=check_installed(d.import_name)) for d in REVIEW_DEPS]


def missing_deps() -> list[DepStatus]:
    return [s for s in status_all() if not s.installed]


def estimated_download_mb(
    missing: list[DepStatus],
    whisper_model: str = "base.en",
) -> int:
    total = sum(s.spec.size_mb for s in missing)
    total += WHISPER_MODEL_SIZES_MB.get(whisper_model, 150)
    return total


def check_hf_token() -> tuple[bool, str]:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if token:
        return True, f"HF token present (length={len(token)})"
    return False, (
        "HuggingFace token not set. pyannote.audio requires a HF token to\n"
        "download diarization weights. Set HF_TOKEN or\n"
        "HUGGING_FACE_HUB_TOKEN before running review. Get one at:\n"
        "  https://huggingface.co/settings/tokens\n"
        "Accept the pyannote model terms at:\n"
        "  https://huggingface.co/pyannote/speaker-diarization-3.1"
    )


def check_ffmpeg() -> tuple[bool, str]:
    if shutil.which("ffmpeg"):
        return True, "ffmpeg present on PATH"
    return False, (
        "ffmpeg not found on PATH. WhisperX requires ffmpeg for audio\n"
        "decoding. Install on Windows:\n"
        "  1. Download from https://www.gyan.dev/ffmpeg/builds/\n"
        "  2. Extract to C:\\ffmpeg\n"
        "  3. Add C:\\ffmpeg\\bin to your PATH\n"
        "Or via chocolatey: choco install ffmpeg"
    )


def print_preinstall_banner(whisper_model: str = "base.en") -> bool:
    """Print a summary of what will be installed. Return True if there is
    anything to do, False if every review dep is already present."""
    statuses = status_all()
    missing = [s for s in statuses if not s.installed]
    print("[review-deps] review dependency summary:")
    for s in statuses:
        marker = "OK     " if s.installed else "MISSING"
        print(f"  [{marker}] {s.spec.pkg:<22} {s.spec.desc} (~{s.spec.size_mb} MB)")
    model_size = WHISPER_MODEL_SIZES_MB.get(whisper_model, 150)
    print(f"[review-deps] WhisperX weights ({whisper_model}): ~{model_size} MB")
    if not missing:
        print("[review-deps] all review deps already installed; nothing to do")
    else:
        total_mb = estimated_download_mb(missing, whisper_model=whisper_model)
        print(f"[review-deps] estimated total new download: ~{total_mb} MB")
    hf_ok, hf_msg = check_hf_token()
    if not hf_ok:
        print("[review-deps] WARNING:")
        print(hf_msg)
    ff_ok, ff_msg = check_ffmpeg()
    if not ff_ok:
        print("[review-deps] WARNING:")
        print(ff_msg)
    return bool(missing)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review deps status + idempotency helpers")
    parser.add_argument(
        "command",
        choices=["status", "summary", "missing", "check-hf", "check-ffmpeg"],
    )
    parser.add_argument("--whisper-model", default="base.en")
    args = parser.parse_args(argv)

    if args.command == "status":
        for s in status_all():
            marker = "1" if s.installed else "0"
            print(f"{marker} {s.spec.pkg}")
        return 0
    if args.command == "missing":
        for s in missing_deps():
            print(s.spec.pkg)
        return 0
    if args.command == "summary":
        has_work = print_preinstall_banner(whisper_model=args.whisper_model)
        return 2 if has_work else 0
    if args.command == "check-hf":
        ok, msg = check_hf_token()
        print(msg)
        return 0 if ok else 1
    if args.command == "check-ffmpeg":
        ok, msg = check_ffmpeg()
        print(msg)
        return 0 if ok else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
