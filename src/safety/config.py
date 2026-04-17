"""Safety config loader."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class RealityAnchorsConfig:
    enabled: bool = True
    rate_denominator: int = 50
    min_turn_gap: int = 8
    phrases: list[str] = field(default_factory=list)
    suppress_when_any_of: list[str] = field(default_factory=list)


@dataclass
class HealthMonitorConfig:
    enabled: bool = True
    daily_minutes_soft_threshold: int = 240
    sustained_days_soft: int = 14
    daily_minutes_stronger_threshold: int = 360
    sustained_days_stronger: int = 28
    repeat_cooldown_days: int = 14


@dataclass
class PIIScrubberConfig:
    enabled: bool = True
    user_name: str = ""
    user_aliases: list[str] = field(default_factory=list)
    child_names: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)
    scrub_emails: bool = True
    scrub_phones: bool = True
    sensitive_tokens: list[str] = field(default_factory=list)


@dataclass
class MemoryEncryptionConfig:
    enabled: bool = False
    keyring_service: str = "renee-aiden"
    keyring_username: str = "renee-memory-key"
    fallback_key_filename: str = ".memory_key"


@dataclass
class SafetyConfig:
    reality_anchors: RealityAnchorsConfig = field(default_factory=RealityAnchorsConfig)
    health_monitor: HealthMonitorConfig = field(default_factory=HealthMonitorConfig)
    pii_scrubber: PIIScrubberConfig = field(default_factory=PIIScrubberConfig)
    memory_encryption: MemoryEncryptionConfig = field(default_factory=MemoryEncryptionConfig)


def load_safety_config(path: str | Path) -> SafetyConfig:
    p = Path(path)
    if not p.exists():
        return SafetyConfig()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    ra = raw.get("reality_anchors") or {}
    hm = raw.get("health_monitor") or {}
    pii = raw.get("pii_scrubber") or {}
    me = raw.get("memory_encryption") or {}
    return SafetyConfig(
        reality_anchors=RealityAnchorsConfig(
            enabled=bool(ra.get("enabled", True)),
            rate_denominator=int(ra.get("rate_denominator", 50)),
            min_turn_gap=int(ra.get("min_turn_gap", 8)),
            phrases=list(ra.get("phrases") or []),
            suppress_when_any_of=list(ra.get("suppress_when_any_of") or []),
        ),
        health_monitor=HealthMonitorConfig(
            enabled=bool(hm.get("enabled", True)),
            daily_minutes_soft_threshold=int(hm.get("daily_minutes_soft_threshold", 240)),
            sustained_days_soft=int(hm.get("sustained_days_soft", 14)),
            daily_minutes_stronger_threshold=int(hm.get("daily_minutes_stronger_threshold", 360)),
            sustained_days_stronger=int(hm.get("sustained_days_stronger", 28)),
            repeat_cooldown_days=int(hm.get("repeat_cooldown_days", 14)),
        ),
        pii_scrubber=PIIScrubberConfig(
            enabled=bool(pii.get("enabled", True)),
            user_name=str(pii.get("user_name") or ""),
            user_aliases=list(pii.get("user_aliases") or []),
            child_names=list(pii.get("child_names") or []),
            addresses=list(pii.get("addresses") or []),
            scrub_emails=bool(pii.get("scrub_emails", True)),
            scrub_phones=bool(pii.get("scrub_phones", True)),
            sensitive_tokens=list(pii.get("sensitive_tokens") or []),
        ),
        memory_encryption=MemoryEncryptionConfig(
            enabled=bool(me.get("enabled", False)),
            keyring_service=str(me.get("keyring_service") or "renee-aiden"),
            keyring_username=str(me.get("keyring_username") or "renee-memory-key"),
            fallback_key_filename=str(me.get("fallback_key_filename") or ".memory_key"),
        ),
    )
