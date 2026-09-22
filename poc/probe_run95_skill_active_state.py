"""Is there a property that says "the action skill is running"?

Better than inferring it from a trick beginning and ending: a flag the game keeps while the
skill is active would need no trick names, survive a missed end, and cover skills whose
tricks are named differently. This samples every plausible property of the pawn, the
controller, the player state and any action skill component four times a second and prints
every value that changes, so using the skill shows which one flips.

Use the action skill twice while it runs, and let it end on its own once and cancel it once
(hold F), so a property that only follows the cooldown can be told from one that follows the
skill itself.
"""
import time, threading, collections
from pathlib import Path
import unrealsdk
from unrealsdk import logging
from unrealsdk.hooks import Type
from mods_base import get_pc

OUT = Path(__file__).parent / "probe_output.txt"
HOOK_ID = "probe95"
TICK_FUNCTION = "/Script/GbxUIUMG.GbxUIUMGTickWidget:BP_TickWidget"
WINDOW_S = 240.0
SAMPLE_INTERVAL_S = 0.25
MAX_CHANGES = 400
NAME_HINT = ("skill", "action", "cooldown", "charge", "duration", "ability", "gadget", "active")
SKIP = ("Location", "Rotation", "Velocity", "Transform", "Time", "Health", "Shield", "Ammo")

_last: dict[str, str] = {}
_changes: list[tuple[float, str, str, str]] = []
_sampled_at = 0.0
_lock = threading.Lock()


def say(m):
    logging.info("[active95] " + m)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(m + "\n")


def property_names(obj):
    try:
        return [str(field.Name) for field in obj.Class._properties()]
    except Exception:
        try:
            return [name for name in dir(obj) if not name.startswith("_")]
        except Exception:
            return []


def interesting(name):
    lowered = name.lower()
    if any(skip.lower() in lowered for skip in SKIP):
        return False
    return any(hint in lowered for hint in NAME_HINT)


def value_text(value):
    try:
        if hasattr(value, "_path_name"):
            return f"{value.Class.Name} {value.Name}"
        text = repr(value)
    except Exception as ex:
        return f"<{type(ex).__name__}>"
    return text[:70]


def sources():
    """(label, object) for everything worth sampling, read fresh every time."""
    found = []
    try:
        controller = get_pc()
    except Exception:
        return found
    found.append(("controller", controller))
    pawn = getattr(controller, "Pawn", None)
    if pawn is not None:
        found.append(("pawn", pawn))
        for name in property_names(pawn):
            if not interesting(name):
                continue
            try:
                value = getattr(pawn, name)
            except Exception:
                continue
            if value is not None and hasattr(value, "Class") and hasattr(value, "_path_name"):
                found.append((f"pawn.{name}", value))
    state = getattr(controller, "PlayerState", None)
    if state is not None:
        found.append(("playerstate", state))
    return found


def sample(now):
    for label, obj in sources():
        for name in property_names(obj):
            if not interesting(name):
                continue
            try:
                text = value_text(getattr(obj, name))
            except Exception:
                continue
            key = f"{label}.{name}"
            with _lock:
                previous = _last.get(key)
                if previous is None:
                    _last[key] = text
                    continue
                if previous != text and len(_changes) < MAX_CHANGES:
                    _last[key] = text
                    _changes.append((now, key, previous, text))


def on_tick(obj, args, ret, func):
    global _sampled_at
    try:
        now = time.time()
        if now - _sampled_at < SAMPLE_INTERVAL_S:
            return
        _sampled_at = now
        sample(now)
    except Exception:
        pass


unrealsdk.hooks.add_hook(TICK_FUNCTION, Type.POST, HOOK_ID, on_tick)
say(f"{time.strftime('%H:%M:%S')} watching every skill-ish property for {WINDOW_S / 60:.0f} minutes")
say("   use the action skill twice: once until it ends by itself, once cancelled with F")


def finish():
    time.sleep(WINDOW_S)
    if unrealsdk.hooks.has_hook(TICK_FUNCTION, Type.POST, HOOK_ID):
        unrealsdk.hooks.remove_hook(TICK_FUNCTION, Type.POST, HOOK_ID)
    with _lock:
        changes = list(_changes)
        watched = len(_last)
    say(f"   watched {watched} propert(ies), {len(changes)} change(s)")
    counts = collections.Counter(key for _when, key, _before, _after in changes)
    say("   by property:")
    for key, count in counts.most_common(25):
        say(f"      {count:>4} x {key}")
    say("   in order:")
    for when, key, before, after in changes[:200]:
        say(f"      {time.strftime('%H:%M:%S', time.localtime(when))} {key:<44} {before} -> {after}")
    say("done")


threading.Thread(target=finish, name="active95", daemon=True).start()
