"""Passive per-turn telemetry tap + ablation-aware recording for the E2E proof.

See ``collector.py`` for the honesty contract. Import surface:

    from src.telemetry import TelemetryCollector, RunConfig
"""
from .collector import (
    ARCHITECTURE_STATUS,
    MATRIX_TEST_TYPES,
    RunConfig,
    TelemetryCollector,
    build_record,
    require_real_backend_for_matrix,
)

__all__ = [
    "ARCHITECTURE_STATUS",
    "MATRIX_TEST_TYPES",
    "RunConfig",
    "TelemetryCollector",
    "build_record",
    "require_real_backend_for_matrix",
]
