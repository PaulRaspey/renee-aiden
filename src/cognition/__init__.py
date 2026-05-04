"""
Cognition layer (M16).

FringeState: a slowly-updating low-dimensional cognitive state that encodes
the *direction* of a conversation rather than its content. Updated between
turns, used as anticipatory bias for retrieval and prompt construction.

Jamesian fringe architecture. Toggleable via FRINGE_ENABLED for A/B.
"""
from .fringe import FringeState, OpenLoop, Turn

__all__ = [
    "FringeState",
    "OpenLoop",
    "Turn",
]
