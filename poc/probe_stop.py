"""Detach every probe hook (print-hooks from probe_damage.py, poller from probe_poll.py) and report counts."""

import builtins
import time
from pathlib import Path

from unrealsdk import hooks, logging

OUT = Path(__file__).parent / "probe_output.txt"


def say(msg: str) -> None:
    logging.info(f"[dmgprobe] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


say(f"{time.strftime('%H:%M:%S')} stopping probes")

# probe_damage.py print-hooks
state = getattr(builtins, "_dmgprobe_state", None)
if state is not None:
    for path in state["hooked"]:
        try:
            if hooks.has_hook(path, hooks.Type.POST, "dmgprobe"):
                hooks.remove_hook(path, hooks.Type.POST, "dmgprobe")
        except Exception:  # noqa: BLE001
            pass
    say(f"   removed {len(state['hooked'])} print-hooks")
    state["hooked"].clear()
    counts = sorted(state["counts"].items(), key=lambda kv: -kv[1])
    for path, n in counts[:20]:
        say(f"   {n:6d}  {path}")
    state["counts"].clear()
    state["prints"].clear()

# probe_poll.py poller
poll = getattr(builtins, "_dmgpoll_state", None)
if poll is not None and poll.get("hooked"):
    try:
        hooks.remove_hook("/Script/GbxUIUMG.GbxUIUMGTickWidget:BP_TickWidget", hooks.Type.POST, "dmgpoll")
    except Exception:  # noqa: BLE001
        pass
    poll["hooked"] = False
    say(f"   poller removed: {poll['ticks']} ticks, {poll['events']} damage instances seen")

# probe_slotfocus.py inventory hooks
slot_hooks = getattr(builtins, "_dmgslotfocus_hooks", None)
if slot_hooks:
    for path in slot_hooks:
        try:
            if hooks.has_hook(path, hooks.Type.PRE, "dmgslotfocus"):
                hooks.remove_hook(path, hooks.Type.PRE, "dmgslotfocus")
        except Exception:  # noqa: BLE001
            pass
    say(f"   removed {len(slot_hooks)} slot focus hooks")
    slot_hooks.clear()

# probe_menu.py sampler
menu_stop = getattr(builtins, "_dmgmenu_stop", None)
if menu_stop is not None:
    try:
        menu_stop()
        say("   menu sampler stopped")
    except Exception as ex:  # noqa: BLE001
        say(f"   menu sampler: {type(ex).__name__}: {ex}")

say(f"done -> {OUT}")
