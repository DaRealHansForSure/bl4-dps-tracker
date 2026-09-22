"""Does the game track who caused a hit?

Three questions in one run:

Phase 1, instantly: what the ring's `DamageSource` field actually is (its struct type and
that type's properties), read from the controller's own ring.

Phase 2, instantly: every function of the player's and an enemy's class chain whose name
looks like damage being dealt or taken, with the parameters each one declares. A function
that carries a causer would end the window guessing for good. Parameters are only listed,
never read: reading an argument of an unknown type crashed the game once.

Phase 3, live, once the hot path is free: `DamageSource` in full for a few hits of every
source kind, so a grenade's area can be compared with a skill's and a gun's.

Play normally while it runs: grenade, action skill, gun.
"""
import re, sys, time, threading, collections
from pathlib import Path
import unrealsdk
from unrealsdk import logging
from mods_base import get_pc

OUT = Path(__file__).parent / "probe_output.txt"
WAIT_LIMIT_S = 600.0
WINDOW_S = 300.0
MAX_PER_SOURCE = 4
DAMAGE_NAMES = re.compile(r"damage|hurt|kill|deal|apply.*hit|hit.*apply", re.IGNORECASE)

_seen = collections.Counter()
_lock = threading.Lock()


def say(m):
    logging.info("[cause] " + m)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(m + "\n")


def text(value, limit=110):
    try:
        return repr(value)[:limit]
    except Exception as ex:
        return f"<{type(ex).__name__}>"


def describe_struct(value, indent="      "):
    say(f"{indent}{type(value).__name__}: {text(value)}")
    try:
        names = [str(field.Name) for field in value._type._properties()]
    except Exception as ex:
        say(f"{indent}   no property list: {type(ex).__name__}: {ex}")
        return
    say(f"{indent}   properties: {names}")
    for name in names:
        try:
            inner = getattr(value, name)
        except Exception as ex:
            say(f"{indent}   {name:<24} <{type(ex).__name__}>")
            continue
        extra = ""
        for detail in ("_path_name", "Name"):
            try:
                attribute = getattr(inner, detail)
                extra = f"  {attribute() if callable(attribute) else attribute}"
                break
            except Exception:
                continue
        say(f"{indent}   {name:<24} {text(inner, 70)}{extra}")


def class_chain(cls):
    chain, seen = [], set()
    while cls is not None and int(cls._get_address()) not in seen:
        seen.add(int(cls._get_address()))
        chain.append(cls)
        cls = getattr(cls, "SuperField", None) or getattr(cls, "Super", None)
    return chain


def damage_functions(cls):
    found = []
    try:
        fields = list(cls._fields())
    except Exception:
        return found
    for field in fields:
        try:
            if str(field.Class.Name) != "Function" or not DAMAGE_NAMES.search(str(field.Name)):
                continue
            try:
                parameters = [f"{p.Class.Name} {p.Name}" for p in field._properties()]
            except Exception:
                parameters = ["?"]
            found.append((str(field.Name), parameters))
        except Exception:
            continue
    return found


say(f"{time.strftime('%H:%M:%S')} does a hit know who caused it?")
controller = get_pc()
ring = getattr(controller, "RecentlyCausedDamageInstances", None)
if ring is not None:
    best = max(range(len(ring)), key=lambda index: float(getattr(ring[index], "TotalDamage", 0.0) or 0.0))
    say(f"   phase 1: the DamageSource of ring slot {best} ({float(ring[best].TotalDamage):.1f} damage)")
    describe_struct(ring[best].DamageSource)

say("   phase 2: functions that look like damage")
pawn = getattr(controller, "Pawn", None)
targets = {"the player's pawn": getattr(pawn, "Class", None), "the controller": getattr(controller, "Class", None)}
try:
    enemy = ring[best].DamagedActor
    if enemy is not None:
        targets["a damaged enemy"] = enemy.Class
except Exception:
    pass
for label, cls in targets.items():
    if cls is None:
        continue
    for step in class_chain(cls):
        functions = damage_functions(step)
        if not functions:
            continue
        say(f"      {step._path_name()} ({label}):")
        for name, parameters in functions:
            say(f"         {name:<34} {parameters}")

mod = sys.modules["dmgtracker.mod"]


def dump(instance, event):
    say(f"   {time.strftime('%H:%M:%S')} {event['dmg']:.1f} {event['kind']} src={event['src']} why={event.get('why')} dtype={event['dtype']} target={event['target']}")
    try:
        describe_struct(instance.DamageSource, "      ")
    except Exception as ex:
        say(f"      DamageSource unreadable: {type(ex).__name__}: {ex}")


def phase_three():
    waited = 0.0
    poller = mod.poller
    while waited < WAIT_LIMIT_S and type(poller._read_instance).__name__ != "method":
        time.sleep(5.0)
        waited += 5.0
        poller = mod.poller
    original = poller._read_instance
    if type(original).__name__ != "method":
        say("   phase 3 skipped: the hot path is held by another probe")
        say("done")
        return

    def wrapped(instance, pawn, now, flagged_targets):
        event = original(instance, pawn, now, flagged_targets)
        try:
            key = event.get("src", "?")
            with _lock:
                room = _seen[key] < MAX_PER_SOURCE
                if room:
                    _seen[key] += 1
            if room:
                dump(instance, event)
        except Exception as ex:
            say(f"   dump failed: {type(ex).__name__}: {ex}")
        return event

    poller._read_instance = wrapped
    say(f"   phase 3: watching {MAX_PER_SOURCE} hit(s) per source kind for {WINDOW_S / 60:.0f} minutes")
    time.sleep(WINDOW_S)
    if poller._read_instance is wrapped:
        poller._read_instance = original
    say(f"   dumps per source: {dict(_seen)}")
    say("done")


threading.Thread(target=phase_three, name="cause-a3", daemon=True).start()
