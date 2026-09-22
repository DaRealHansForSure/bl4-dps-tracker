"""Do the engine's damage events fire in this game?

`OakPlayerController:ReceiveInstigatedAnyDamage` declares (damage, DamageType,
DamagedActor, DamageCauser) and `Actor:ReceiveAnyDamage` declares (damage, DamageType,
InstigatedBy, DamageCauser). If either fires when the player deals damage, the mod can read
the causer instead of guessing from windows.

This only counts calls and reads the calling object's class name. No argument is touched:
reading an argument of an unknown property type crashed the game once, and a crash costs a
session. If they fire, the next probe reads the causer.

Play for five minutes: gun, grenade, action skill, melee.
"""
import time, threading, collections
from pathlib import Path
import unrealsdk
from unrealsdk import logging
from unrealsdk.hooks import Type

OUT = Path(__file__).parent / "probe_output.txt"
HOOK_ID = "probea4"
WINDOW_S = 300.0

FUNCTIONS = (
    "/Script/OakGame.OakPlayerController:ReceiveInstigatedAnyDamage",
    "/Script/Engine.Controller:ReceiveInstigatedAnyDamage",
    "/Script/Engine.Actor:ReceiveAnyDamage",
    "/Script/Engine.Actor:ReceivePointDamage",
    "/Script/Engine.Actor:ReceiveRadialDamage",
    "/Script/OakGame.OakCharacter:ReceiveAnyDamage",
    "/Script/OakGame.OakCharacter:ReceivePointDamage",
    "/Script/Engine.Actor:TakeDamage",
    "/Script/GbxGame.GbxCharacter:ReceiveAnyDamage",
)

_counts = collections.Counter()
_first = {}
_lock = threading.Lock()


def say(m):
    logging.info("[events] " + m)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(m + "\n")


def make_callback(path):
    def callback(obj, args, ret, func):
        try:
            with _lock:
                _counts[path] += 1
                seen = _counts[path]
            if seen <= 3:
                try:
                    what = f"{obj.Class.Name} {obj.Name}"
                except Exception:
                    what = "?"
                say(f"      {time.strftime('%H:%M:%S')} {path.rsplit(':', 1)[1]} on {what}")
                with _lock:
                    _first.setdefault(path, time.time())
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

say(f"{time.strftime('%H:%M:%S')} {len(installed)} damage event(s) hooked for {WINDOW_S / 60:.0f} minutes; arguments are not read")
say("   play: gun, grenade, action skill, melee")


def finish():
    time.sleep(WINDOW_S)
    for path, identifier in installed:
        try:
            if unrealsdk.hooks.has_hook(path, Type.PRE, identifier):
                unrealsdk.hooks.remove_hook(path, Type.PRE, identifier)
        except Exception as ex:
            say(f"   cannot remove the hook on {path}: {type(ex).__name__}: {ex}")
    with _lock:
        counts = dict(_counts)
    say(f"   over: {sum(counts.values())} call(s)")
    if not counts:
        say("      none of the engine's damage events fire in this game; the ring stays the only source")
    for path, count in sorted(counts.items(), key=lambda pair: -pair[1]):
        say(f"      {count:>6} x {path}")
    say("done")


threading.Thread(target=finish, name="events-a4", daemon=True).start()
