"""What changes while the shoulder cannons are out?

The cannons keep firing for 18 s while the gun stays in hand, and the mod has no signal for
that: their damage (26.5 Normal + 9.9 Fire, seven pairs a second) lands on the skill window
a passive keeps open, or on Other when it lapses.

Candidates this samples four times a second, printing every change:
  - the weapon slots themselves (count and each slot's weapon class and body), in case the
    cannons are a weapon of their own the mod could name;
  - `PlayerState.ActiveWeaponRestoreEquipSlots`, which flipped 0 -> 2 around two earlier
    skill uses (probe 95);
  - `pawn.ActionSkillLock`, the equipment locks, and the action skill cooldown if there is
    one.

Use the action skill once, let it run out on its own, and shoot a little afterwards.
"""
import time, threading, collections
from pathlib import Path
import unrealsdk
from unrealsdk import logging
from unrealsdk.hooks import Type
from mods_base import get_pc

OUT = Path(__file__).parent / "probe_output.txt"
HOOK_ID = "probeb1"
TICK_FUNCTION = "/Script/GbxUIUMG.GbxUIUMGTickWidget:BP_TickWidget"
WINDOW_S = 240.0
SAMPLE_INTERVAL_S = 0.25

_last = {}
_changes = []
_sampled_at = 0.0
_lock = threading.Lock()


def say(m):
    logging.info("[cannons] " + m)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(m + "\n")


def note(now, key, before, after):
    with _lock:
        if len(_changes) < 400:
            _changes.append((now, key, before, after))


def weapon_text(weapon):
    if weapon is None:
        return "none"
    try:
        body = weapon.BodyData
        return f"{weapon.Class.Name} {body._path_name().rsplit('.', 1)[-1] if body is not None else '?'}"
    except Exception:
        try:
            return str(weapon.Class.Name)
        except Exception:
            return "?"


def readings():
    """(key, text) for everything worth watching, read fresh."""
    out = []
    try:
        controller = get_pc()
    except Exception:
        return out
    pawn = getattr(controller, "Pawn", None)
    if pawn is None:
        return out
    try:
        slots = pawn.ActiveWeapons.Slots
        out.append(("ActiveWeapons.count", str(len(slots))))
        for index, slot in enumerate(slots):
            out.append((f"ActiveWeapons[{index}]", weapon_text(getattr(slot, "Weapon", None))))
    except Exception as ex:
        out.append(("ActiveWeapons", f"<{type(ex).__name__}>"))
    try:
        items = pawn.EquippedInventorySlots.items
        out.append(("EquippedInventorySlots.count", str(len(items))))
    except Exception:
        pass
    for label, obj, name in (("pawn", pawn, "ActionSkillLock"), ("pawn", pawn, "AllowForcedGadgetUse"),
                             ("pawn", pawn, "EquipmentLocks"), ("pawn", pawn, "OneHandWeaponActionLock"),
                             ("state", getattr(controller, "PlayerState", None), "ActiveWeaponRestoreEquipSlots")):
        if obj is None:
            continue
        try:
            out.append((f"{label}.{name}", repr(getattr(obj, name))[:70]))
        except Exception:
            continue
    return out


def on_tick(obj, args, ret, func):
    global _sampled_at
    try:
        now = time.time()
        if now - _sampled_at < SAMPLE_INTERVAL_S:
            return
        _sampled_at = now
        for key, text in readings():
            with _lock:
                previous = _last.get(key)
                _last[key] = text
            if previous is not None and previous != text:
                note(now, key, previous, text)
    except Exception:
        pass


unrealsdk.hooks.add_hook(TICK_FUNCTION, Type.POST, HOOK_ID, on_tick)
say(f"{time.strftime('%H:%M:%S')} watching the weapon slots and the skill locks for {WINDOW_S / 60:.0f} minutes")
say("   use the action skill, let it run out, then shoot")


def finish():
    time.sleep(WINDOW_S)
    if unrealsdk.hooks.has_hook(TICK_FUNCTION, Type.POST, HOOK_ID):
        unrealsdk.hooks.remove_hook(TICK_FUNCTION, Type.POST, HOOK_ID)
    with _lock:
        changes = list(_changes)
        watched = sorted(_last)
    say(f"   watched: {watched}")
    say(f"   {len(changes)} change(s):")
    for when, key, before, after in changes:
        say(f"      {time.strftime('%H:%M:%S', time.localtime(when))} {key:<32} {before} -> {after}")
    say("done")


threading.Thread(target=finish, name="cannons-b1", daemon=True).start()
