"""Every property that changes while the action skill runs.

Probe 95 watched only names containing "skill" or "action" and found two: the grenade
throw lock, and `PlayerState.ActiveWeaponRestoreEquipSlots`, which flipped 0 -> 2 while the
Exo Soldier's cannons were out. That one is a side effect of a skill that replaces the
weapon; a skill that does not would not show it. So this watches every readable scalar of
the pawn, the controller, the player state and the pawn's components, twice a second, and
prints what changes, with a marker whenever the mod's own skill hooks fire.

Use the action skill once and let it run out, then throw a grenade, then shoot. The output
lines up the changes with the skill's start and end.
"""
import sys, time, threading, collections
from pathlib import Path
import unrealsdk
from unrealsdk import logging
from unrealsdk.hooks import Type
from mods_base import get_pc

OUT = Path(__file__).parent / "probe_output.txt"
HOOK_ID = "probe99"
TICK_FUNCTION = "/Script/GbxUIUMG.GbxUIUMGTickWidget:BP_TickWidget"
WINDOW_S = 300.0
SAMPLE_INTERVAL_S = 0.5
MAX_CHANGES = 900
MAX_PROPERTIES = 400
# Properties that change every frame and say nothing about skills.
NOISY = ("location", "rotation", "velocity", "transform", "aim", "time", "tick", "frame",
         "health", "shield", "ammo", "yaw", "pitch", "roll", "speed", "physics", "camera",
         "bounds", "matrix", "quat", "seed", "random", "input", "view", "eye")
SCALAR = (bool, int, float, str)

_last = {}
_changes = []
_sampled_at = 0.0
_lock = threading.Lock()


def say(m):
    logging.info("[wide99] " + m)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(m + "\n")


def note(now, key, before, after):
    with _lock:
        if len(_changes) < MAX_CHANGES:
            _changes.append((now, key, before, after))


def property_names(obj):
    try:
        return [str(field.Name) for field in obj.Class._properties()]
    except Exception:
        return []


def wanted(name):
    lowered = name.lower()
    return not any(noise in lowered for noise in NOISY)


def value_text(value):
    if isinstance(value, SCALAR):
        return repr(value)[:60]
    try:
        text = repr(value)
    except Exception:
        return None
    if len(text) > 70 or "\n" in text:
        return None
    return text


def sources():
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
            if not wanted(name):
                continue
            try:
                value = getattr(pawn, name)
            except Exception:
                continue
            if value is not None and hasattr(value, "Class") and hasattr(value, "_path_name"):
                found.append((f"pawn.{name}", value))
    state = getattr(controller, "PlayerState", None)
    if state is not None:
        found.append(("state", state))
    return found


def sample(now):
    seen = 0
    for label, obj in sources():
        for name in property_names(obj):
            if not wanted(name) or seen >= MAX_PROPERTIES:
                continue
            try:
                text = value_text(getattr(obj, name))
            except Exception:
                continue
            if text is None:
                continue
            seen += 1
            key = f"{label}.{name}"
            with _lock:
                previous = _last.get(key)
                _last[key] = text
            if previous is not None and previous != text:
                note(now, key, previous, text)


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


# The mod's own signals, so the changes can be read against them.
mod = sys.modules["dmgtracker.mod"]
poller = mod.poller
watched = ("note_skill_started", "note_skill_ended", "note_skill_activity", "note_grenade_thrown", "note_grenade_exploded")
originals = {}
for name in watched:
    if not hasattr(poller, name):
        continue
    originals[name] = getattr(poller, name)

    def make(name, original):
        def wrapper(now, *rest):
            note(now, f"SIGNAL {name}", "", str(rest[0])[:40] if rest else "")
            return original(now, *rest)
        return wrapper

    setattr(poller, name, make(name, originals[name]))

unrealsdk.hooks.add_hook(TICK_FUNCTION, Type.POST, HOOK_ID, on_tick)
say(f"{time.strftime('%H:%M:%S')} watching every readable property for {WINDOW_S / 60:.0f} minutes")
say("   use the action skill and let it run out, then throw a grenade, then shoot")


def finish():
    time.sleep(WINDOW_S)
    if unrealsdk.hooks.has_hook(TICK_FUNCTION, Type.POST, HOOK_ID):
        unrealsdk.hooks.remove_hook(TICK_FUNCTION, Type.POST, HOOK_ID)
    for name, original in originals.items():
        setattr(poller, name, original)
    with _lock:
        changes = list(_changes)
        watched_count = len(_last)
    say(f"   watched {watched_count} propert(ies), {len(changes)} change(s)")
    counts = collections.Counter(key for _when, key, _before, _after in changes)
    say("   by property (a skill flag changes twice per use, not every frame):")
    for key, count in counts.most_common(30):
        say(f"      {count:>4} x {key}")
    say("   in order:")
    for when, key, before, after in changes:
        say(f"      {time.strftime('%H:%M:%S', time.localtime(when))} {key:<52} {before} -> {after}")
    say("done")


threading.Thread(target=finish, name="wide99", daemon=True).start()
