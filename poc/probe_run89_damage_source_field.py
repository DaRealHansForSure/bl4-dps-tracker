"""Does a hit name its own source?

Probe 88 showed the ring struct's seven properties: DamageType (an FGameDataHandle around
an OakDamageTypeDef), DamageSource (a struct, empty for the gun hit that was in the ring),
DamagedActor, DamageLocation, TotalDamage, DamageFlags, RepDirtyFlag.

This dumps DamageSource in full, plus the damage type's handle id, name and gameplay tags,
for a few hits of each source kind. If DamageSource is filled for a grenade's area or a
skill, or if the tags name them, the mod can stop guessing from windows altogether.

It waits until probe 87's wrapper is gone before wrapping `_read_instance` itself, so the
two do not fight over the hot path. Play for a few minutes: grenade, skill, gun, melee.
"""
import sys, time, threading, collections
from pathlib import Path
from unrealsdk import logging

OUT = Path(__file__).parent / "probe_output.txt"
WAIT_LIMIT_S = 900.0
WINDOW_S = 420.0
MAX_PER_SOURCE = 5
MAX_VALUE_CHARS = 150

_seen = collections.Counter()
_lock = threading.Lock()


def say(m):
    logging.info("[source89] " + m)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(m + "\n")


def text(value):
    try:
        return repr(value)[:MAX_VALUE_CHARS]
    except Exception as ex:
        return f"<{type(ex).__name__}>"


def describe(value, indent="         "):
    """A struct or handle in full: its type, then every field that is not a method."""
    say(f"{indent}type {type(value).__name__}: {text(value)}")
    for name in sorted(n for n in dir(value) if not n.startswith("_")):
        try:
            field = getattr(value, name)
        except Exception as ex:
            say(f"{indent}   {name:<26} <{type(ex).__name__}>")
            continue
        if callable(field):
            continue
        say(f"{indent}   {name:<26} {text(field)}")


def dump(instance, event):
    say(f"   {time.strftime('%H:%M:%S')} {event['dmg']:.1f} {event['kind']} src={event['src']} why={event.get('why')} dtype={event['dtype']} target={event['target']}")
    try:
        say(f"      DamageSource: {text(instance.DamageSource)}")
        describe(instance.DamageSource, "      ")
    except Exception as ex:
        say(f"      DamageSource unreadable: {type(ex).__name__}: {ex}")
    try:
        handle = instance.DamageType
        say(f"      DamageType handle: {text(handle)}")
        for name in ("GameplayTags", "HealthTypeTags", "StatusEffectData", "DamageIconFrameName", "ElementalType", "ModifierAttributes"):
            try:
                say(f"         {name:<26} {text(getattr(handle, name))}")
            except Exception as ex:
                say(f"         {name:<26} <{type(ex).__name__}>")
    except Exception as ex:
        say(f"      DamageType unreadable: {type(ex).__name__}: {ex}")


mod = sys.modules["dmgtracker.mod"]
poller = mod.poller


def wrap_when_free():
    waited = 0.0
    while waited < WAIT_LIMIT_S and type(poller._read_instance).__name__ != "method":
        time.sleep(5.0)
        waited += 5.0
    original = poller._read_instance
    if type(original).__name__ != "method":
        say("   another probe still holds the hot path; giving up")
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
        except Exception as ex:  # the probe may never break the mod's hot path
            say(f"   dump failed: {type(ex).__name__}: {ex}")
        return event

    poller._read_instance = wrapped
    say(f"   {time.strftime('%H:%M:%S')} wrapped; {MAX_PER_SOURCE} hit(s) per source kind, {WINDOW_S / 60:.0f} minutes")
    time.sleep(WINDOW_S)
    poller._read_instance = original
    say(f"   unwrapped; dumps per source: {dict(_seen)}")
    say("done")


say(f"{time.strftime('%H:%M:%S')} does a hit name its own source? waiting for the hot path to be free")
threading.Thread(target=wrap_when_free, name="source89", daemon=True).start()
