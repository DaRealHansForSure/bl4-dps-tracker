"""Which skill script is the action skill, and which are the passives?

`GbxSkillScript:K2_StartSkillAction` fires many times a second during a fight (98 times in
two minutes), which is why the mod's skill window is useless on its own. But every call
carries the script object, and its class name has never been read. If the action skill's
script is named differently from the passives', the mod gets the precise signal it needs:
open the skill on that class, ignore the rest.

The same for the tricks, whose classes are read but only matched against "ActionSkill", and
for `GbxSkillTokenProviderStatics:AddToTokenStack`, whose calls are the kill stacks.

Every distinct class name is logged once with its first and last time and its count, so a
run where the action skill is used twice shows which name appears exactly then.

Use the action skill twice with a gap, shoot in between, and say roughly when.
"""
import time, threading, collections
from pathlib import Path
import unrealsdk
from unrealsdk import logging
from unrealsdk.hooks import Type
from mods_base import get_pc

OUT = Path(__file__).parent / "probe_output.txt"
HOOK_ID = "probeb3"
WINDOW_S = 240.0

FUNCTIONS = (
    ("skill-action", "/Script/GbxGame.GbxSkillScript:K2_StartSkillAction"),
    ("trick-begin", "/Script/GbxGame.GbxTrickScript:OnBegin_Mut"),
    ("trick-end", "/Script/GbxGame.GbxTrickScript:OnEnd_Mut"),
    ("token", "/Script/OakGame.GbxSkillTokenProviderStatics:AddToTokenStack"),
)

_seen = {}
_lock = threading.Lock()


def say(m):
    logging.info("[scripts] " + m)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(m + "\n")


def owner_kind(obj):
    """Whether the player controller owns this script, which tells the player's skills from
    everyone else's."""
    try:
        owner = obj.GetOwner()
    except Exception:
        return "no owner getter"
    if owner is None:
        return "no owner"
    try:
        if int(owner._get_address()) == int(get_pc()._get_address()):
            return "the player"
        return f"{owner.Class.Name}"
    except Exception:
        return "?"


def make_callback(label):
    def callback(obj, args, ret, func):
        try:
            try:
                name = str(obj.Class.Name)
            except Exception:
                name = "?"
            key = (label, name, owner_kind(obj))
            now = time.time()
            with _lock:
                entry = _seen.get(key)
                if entry is None:
                    _seen[key] = [1, now, now]
                else:
                    entry[0] += 1
                    entry[2] = now
        except Exception:
            pass
    return callback


installed = []
for label, path in FUNCTIONS:
    identifier = f"{HOOK_ID}:{label}"
    try:
        unrealsdk.hooks.add_hook(path, Type.PRE, identifier, make_callback(label))
        installed.append((path, identifier))
    except Exception as ex:
        say(f"   cannot hook {path}: {type(ex).__name__}: {ex}")

say(f"{time.strftime('%H:%M:%S')} skill script classes; {len(installed)} hook(s) for {WINDOW_S / 60:.0f} minutes")
say("   use the action skill twice with a gap, shoot in between")


def finish():
    time.sleep(WINDOW_S)
    for path, identifier in installed:
        try:
            if unrealsdk.hooks.has_hook(path, Type.PRE, identifier):
                unrealsdk.hooks.remove_hook(path, Type.PRE, identifier)
        except Exception:
            pass
    with _lock:
        seen = dict(_seen)
    say(f"   {len(seen)} distinct script class(es):")
    for (label, name, owner), (count, first, last) in sorted(seen.items(), key=lambda pair: -pair[1][0]):
        span = last - first
        say(f"      {count:>5} x {label:<12} {name:<58} owner {owner:<18} "
            f"{time.strftime('%H:%M:%S', time.localtime(first))} to {time.strftime('%H:%M:%S', time.localtime(last))} ({span:.0f} s)")
    say("   a class that appears only while the skill runs is the signal the mod needs")
    say("done")


threading.Thread(target=finish, name="scripts-b3", daemon=True).start()
