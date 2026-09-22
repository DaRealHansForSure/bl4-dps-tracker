"""The action skill's own signal, and the state it leaves behind.

Probe a3 listed three functions that are about the action skill itself rather than about
skills in general:

    OakCharacter:ClientSetActiveActionSkill(SkillDef)
    OakCharacter:OnRep_OnActionSkillLock()
    OakCharacter:PossessedBySkill(PossessingActor)

`OnRep_OnActionSkillLock` implies a replicated property, which would be the state the mod
should read instead of guessing from windows. This hooks the three (counting calls and the
calling object only, no arguments) and samples every pawn and controller property whose
name mentions the action skill, four times a second, printing changes.

Two clean runs, please, one thing at a time:
  1. use the action skill, let it run out, do nothing else;
  2. throw the UAV grenade, do nothing else.
Then the two are told apart for good.
"""
import re, sys, time, threading, collections
from pathlib import Path
import unrealsdk
from unrealsdk import logging
from unrealsdk.hooks import Type
from mods_base import get_pc

OUT = Path(__file__).parent / "probe_output.txt"
HOOK_ID = "probea7"
TICK_FUNCTION = "/Script/GbxUIUMG.GbxUIUMGTickWidget:BP_TickWidget"
WINDOW_S = 300.0
SAMPLE_INTERVAL_S = 0.25
WANTED = re.compile(r"actionskill|skilllock|possessedby|gadget", re.IGNORECASE)

FUNCTIONS = (
    "/Script/OakGame.OakCharacter:ClientSetActiveActionSkill",
    "/Script/OakGame.OakCharacter:OnRep_OnActionSkillLock",
    "/Script/OakGame.OakCharacter:PossessedBySkill",
    "/Script/OakGame.OakCharacter:OnRep_EquipmentLocks",
    "/Script/OakGame.OakCharacter:UnlockEquipmentActions",
    "/Script/OakGame.OakCharacter:LockEquipmentActions",
    "/Script/OakGame.OakCharacter:ThrowGadget",
)

_events = []
_last = {}
_sampled_at = 0.0
_lock = threading.Lock()


def say(m):
    logging.info("[skillsig] " + m)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(m + "\n")


def note(now, what, detail=""):
    with _lock:
        if len(_events) < 600:
            _events.append((now, what, detail))


def make_callback(path):
    short = path.rsplit(":", 1)[1]

    def callback(obj, args, ret, func):
        try:
            try:
                what = str(obj.Class.Name)
            except Exception:
                what = "?"
            note(time.time(), f"CALL {short}", what)
        except Exception:
            pass
    return callback


installed = []
for path in FUNCTIONS:
    identifier = f"{HOOK_ID}:{path}"
    try:
        unrealsdk.hooks.add_hook(path, Type.PRE, identifier, make_callback(path))
        installed.append((path, identifier))
    except Exception as ex:
        say(f"   cannot hook {path}: {type(ex).__name__}: {ex}")


def property_names(obj):
    try:
        return [str(field.Name) for field in obj.Class._properties()]
    except Exception:
        return []


def sample(now):
    try:
        controller = get_pc()
    except Exception:
        return
    for label, obj in (("pawn", getattr(controller, "Pawn", None)), ("controller", controller),
                       ("state", getattr(controller, "PlayerState", None))):
        if obj is None:
            continue
        for name in property_names(obj):
            if not WANTED.search(name):
                continue
            try:
                text = repr(getattr(obj, name))[:70]
            except Exception:
                continue
            key = f"{label}.{name}"
            with _lock:
                previous = _last.get(key)
                _last[key] = text
            if previous is not None and previous != text:
                note(now, key, f"{previous} -> {text}")


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
say(f"{time.strftime('%H:%M:%S')} the action skill's own signal; {len(installed)} hook(s), {WINDOW_S / 60:.0f} minutes")
say("   first use ONLY the action skill and let it run out, then throw ONLY the UAV grenade")


def finish():
    time.sleep(WINDOW_S)
    if unrealsdk.hooks.has_hook(TICK_FUNCTION, Type.POST, HOOK_ID):
        unrealsdk.hooks.remove_hook(TICK_FUNCTION, Type.POST, HOOK_ID)
    for path, identifier in installed:
        try:
            if unrealsdk.hooks.has_hook(path, Type.PRE, identifier):
                unrealsdk.hooks.remove_hook(path, Type.PRE, identifier)
        except Exception:
            pass
    with _lock:
        events = list(_events)
        watched = sorted(_last)
    say(f"   watched {len(watched)} propert(ies): {watched}")
    say(f"   {len(events)} event(s):")
    for when, what, detail in events:
        say(f"      {time.strftime('%H:%M:%S', time.localtime(when))} {what:<44} {detail}")
    say("done")


threading.Thread(target=finish, name="skillsig-a7", daemon=True).start()
