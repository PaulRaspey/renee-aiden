"""
Safety layer (M13).

Four sub-layers, composed in `SafetyLayer`:
  - RealityAnchorInjector  — soft, ~1-in-50 reality anchors woven into responses
  - HealthMonitor          — daily interaction-time tracking, flag surface
  - PIIScrubber            — tokenize PII before cloud LLM calls, detokenize on return
  - MemoryVault            — AES-256-GCM wrapper around the memory store

Load with `SafetyLayer.from_config(configs/safety.yaml, state_dir)`.
"""
from __future__ import annotations

from .config import SafetyConfig, load_safety_config
from .health_monitor import HealthFlag, HealthMonitor
from .memory_crypto import MemoryVault, derive_key, encrypt, decrypt
from .pii_scrubber import PIIScrubber, ScrubResult
from .reality_anchors import RealityAnchorInjector
from .facade import SafetyLayer

__all__ = [
    "SafetyConfig",
    "SafetyLayer",
    "load_safety_config",
    "HealthFlag",
    "HealthMonitor",
    "MemoryVault",
    "PIIScrubber",
    "RealityAnchorInjector",
    "ScrubResult",
    "derive_key",
    "encrypt",
    "decrypt",
]
