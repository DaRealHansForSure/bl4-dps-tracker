"""Remove the on-screen text widget created by probe_ui.py."""

from __future__ import annotations

import builtins
import time
from pathlib import Path

from unrealsdk import logging

OUT = Path(__file__).parent / "probe_output.txt"


def say(msg: str) -> None:
    logging.info(f"[dmgui] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


say(f"{time.strftime('%H:%M:%S')} ui probe off")
widget = getattr(builtins, "_dmgui_widget", None)
if widget is None:
    say("   no widget to remove")
else:
    try:
        widget.RemoveFromParent()
        say("   widget removed")
    except Exception as ex:  # noqa: BLE001
        say(f"   RemoveFromParent: {type(ex).__name__}: {ex}")
    builtins._dmgui_widget = None  # type: ignore[attr-defined]
