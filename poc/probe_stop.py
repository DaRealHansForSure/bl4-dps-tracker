"""Detach the probe hooks and print how often each hooked function fired."""

import builtins
import time
from pathlib import Path

from unrealsdk import hooks, logging

OUT = Path(__file__).parent / "probe_output.txt"
HOOK_ID = "dmgprobe"

state = getattr(builtins, "_dmgprobe_state", None)
if state is None:
    logging.info("[dmgprobe] nothing to stop")
else:
    for path in state["hooked"]:
        try:
            if hooks.has_hook(path, hooks.Type.POST, HOOK_ID):
                hooks.remove_hook(path, hooks.Type.POST, HOOK_ID)
        except Exception:  # noqa: BLE001
            pass
    state["hooked"].clear()

    counts = sorted(state["counts"].items(), key=lambda kv: -kv[1])
    with OUT.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} probe stop; call counts:\n")
        for path, n in counts:
            f.write(f"   {n:6d}  {path}\n")
    for path, n in counts[:15]:
        logging.info(f"[dmgprobe] {n:6d}  {path}")
    state["counts"].clear()
    state["prints"].clear()
    logging.info(f"[dmgprobe] hooks removed; full output in {OUT}")
