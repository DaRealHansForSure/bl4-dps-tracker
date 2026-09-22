"""Which signal opens the skill window, and when, next to the grenade's signals.

The mod treats four events as "the action skill is active": a trick whose class contains
ActionSkill beginning or ending, `GbxSkillScript:K2_StartSkillAction` on a script the
player controller owns, and any `GbxSkillTokenProviderStatics:AddToTokenStack` (a static
call, so it is not checked against an owner). A grenade opens its own window on
`OakCharacter:ThrowGadget` and `GrenadeGadget:ExplodeCallback`.

This logs all of them for 180 s with a timeline by the second, so a fight in which grenade
damage was credited to the skill shows which signal was responsible. Hooks read the object
only (reading an argument of an unknown type crashed the game once).

Throw a grenade into a fight, do NOT use the action skill, and let the grenade burn.
"""
import time, threading, collections
from pathlib import Path
import unrealsdk
from unrealsdk import logging
from unrealsdk.hooks import Type

OUT = Path(__file__).parent / "probe_output.txt"
HOOK_ID = "probe85-signals"
WINDOW_S = 180.0
MAX_EVENTS = 6000

SIGNALS = (
    ("trick-begin", "/Script/GbxGame.GbxTrickScript:OnBegin_Mut"),
    ("trick-end", "/Script/GbxGame.GbxTrickScript:OnEnd_Mut"),
    ("skill-action", "/Script/GbxGame.GbxSkillScript:K2_StartSkillAction"),
    ("skill-token", "/Script/OakGame.GbxSkillTokenProviderStatics:AddToTokenStack"),
    ("grenade-throw", "/Script/OakGame.OakCharacter:ThrowGadget"),
    ("grenade-explode", "/Script/OakGame.GrenadeGadget:ExplodeCallback"),
    ("projectile-explode", "/Script/GbxGame.projectile:Explode"),
)

_events = []
_lock = threading.Lock()
_started = time.time()


def say(m):
    logging.info("[signals85] " + m)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(m + "\n")


def make_callback(name):
    def callback(obj, args, ret, func):
        try:
            try:
                what = str(obj.Class.Name)
            except Exception:
                what = "?"
            with _lock:
                if len(_events) < MAX_EVENTS:
                    _events.append((time.time(), name, what))
        except Exception:
            pass
    return callback


installed = []
for name, path in SIGNALS:
    identifier = f"{HOOK_ID}:{name}"
    try:
        unrealsdk.hooks.add_hook(path, Type.PRE, identifier, make_callback(name))
        installed.append((path, identifier))
    except Exception as ex:
        say(f"   cannot hook {path}: {type(ex).__name__}: {ex}")

say(f"{time.strftime('%H:%M:%S')} attribution signals; {len(installed)} hook(s) for {WINDOW_S:.0f} s")
say("   throw a grenade into a fight and do not use the action skill")


def finish():
    time.sleep(WINDOW_S)
    for path, identifier in installed:
        try:
            if unrealsdk.hooks.has_hook(path, Type.PRE, identifier):
                unrealsdk.hooks.remove_hook(path, Type.PRE, identifier)
        except Exception as ex:
            say(f"   cannot remove the hook on {path}: {type(ex).__name__}: {ex}")
    with _lock:
        events = list(_events)
    say(f"   hooks removed; {len(events)} signal(s)")
    totals = collections.Counter(name for _when, name, _what in events)
    for name, count in totals.most_common():
        say(f"      {count:>6} x {name}")
    classes = collections.Counter((name, what) for _when, name, what in events)
    say("   by calling class:")
    for (name, what), count in classes.most_common(25):
        say(f"      {count:>6} x {name} on {what}")
    say("   timeline by the second (only seconds with signals):")
    per_second = collections.defaultdict(collections.Counter)
    for when, name, _what in events:
        per_second[int(when)][name] += 1
    for second in sorted(per_second):
        text = " ".join(f"{name}x{count}" for name, count in sorted(per_second[second].items()))
        say(f"      {time.strftime('%H:%M:%S', time.localtime(second))} {text}")
    say("done")


threading.Thread(target=finish, name="signals85", daemon=True).start()
