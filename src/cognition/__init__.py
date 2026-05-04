"""
Cognition layer (M16).

FringeState: a slowly-updating low-dimensional cognitive state that encodes
the *direction* of a conversation rather than its content. Updated between
turns, used as anticipatory bias for retrieval and prompt construction.

Jamesian fringe architecture. Toggleable via FRINGE_ENABLED for A/B.
"""
from .fringe import FringeState, OpenLoop, Turn
from .affect_scorer import AffectScorer
from .register_detector import RegisterDetector
from .loop_tracker import LoopTracker
from .pressure_computer import PressureComputer

__all__ = [
    "FringeState",
    "OpenLoop",
    "Turn",
    "AffectScorer",
    "RegisterDetector",
    "LoopTracker",
    "PressureComputer",
]
