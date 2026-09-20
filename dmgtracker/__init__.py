"""
BL4 Damage Tracker.

Records every damage instance the player deals to a JSONL session log and shows a per-weapon
damage table on screen. The SDK loads this package as a mod; the same package is imported
outside the game by the tests and by report.py, where the SDK modules do not exist. The pure
modules (attribution, formatting, logwriter, poller, stats, report) never import the SDK, and
the SDK glue in mod.py is only loaded when the SDK is present.
"""

from __future__ import annotations

try:
    import unrealsdk  # noqa: F401
except ImportError:
    RUNNING_IN_GAME = False
else:
    RUNNING_IN_GAME = True
    from .mod import mod  # noqa: F401  (importing registers the mod with the SDK)
