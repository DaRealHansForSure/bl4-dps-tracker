"""Everything the damage ring says about a hit, to find a field that names the source.

The mod reads four fields of `RecentPlayerCausedDamageInstance` (flags, damage type,
damaged actor, total damage) and attributes the rest by windows, which is what credited a
grenade's corrosive cloud to the action skill. This dumps every field of the struct for the
next hits, plus the damage type's full asset path and class, so a field that tells a
grenade's damage from a skill's can be found.

It wraps the mod's own `_read_instance` (no second ring reader, no extra hook) and puts it
back when the window is over. Values are read one by one with try/except: some instances
carry an unresolved damage type whose fields raise.

Throw a grenade, let it burn, then use the action skill, then shoot: one of each.
"""
import sys, time, threading
from pathlib import Path
from unrealsdk import logging

OUT = Path(__file__).parent / "probe_output.txt"
WINDOW_S = 180.0
MAX_DUMPS = 40
MAX_VALUE_CHARS = 90

_dumps = 0
_lock = threading.Lock()


def say(m):
    logging.info("[fields86] " + m)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(m + "\n")


def value_text(value):
    try:
        if value is None:
            return "None"
        text = repr(value)
    except Exception as ex:
        return f"<{type(ex).__name__}>"
    return text[:MAX_VALUE_CHARS]


def field_names(struct):
    names = []
    try:
        for field in struct._type._fields():
            try:
                if str(field.Class.Name) != "Function":
                    names.append(str(field.Name))
            except Exception:
                continue
    except Exception as ex:
        say(f"   cannot list the struct's fields: {type(ex).__name__}: {ex}")
    return names


def object_text(value):
    """An object field as class plus path, which is what would tell sources apart."""
    try:
        return f"{value.Class.Name} {value._path_name()}"
    except Exception:
        return value_text(value)


def dump(instance, event):
    say(f"   hit {event['dmg']:.1f} {event['kind']} src={event['src']} dtype={event['dtype']} target={event['target']} flags={event['flags']}")
    for name in field_names(instance):
        try:
            value = getattr(instance, name)
        except Exception as ex:
            say(f"      {name:<28} <{type(ex).__name__}: {ex}>"[:200])
            continue
        if hasattr(value, "_path_name"):
            say(f"      {name:<28} {object_text(value)}"[:220])
        else:
            say(f"      {name:<28} {value_text(value)}")
    try:
        damage_type = instance.DamageType
        say(f"      DamageType details          class {damage_type.Class.Name}, path {damage_type._path_name()}")
        for name in ("ElementalType", "bIsDoT", "bIsHealingType", "StatusEffectType", "DamageSourceType"):
            try:
                say(f"         {name:<24} {value_text(getattr(damage_type, name))}")
            except Exception as ex:
                say(f"         {name:<24} <{type(ex).__name__}>")
    except Exception as ex:
        say(f"      DamageType unreadable: {type(ex).__name__}: {ex}")


mod = sys.modules["dmgtracker.mod"]
poller = mod.poller
original = poller._read_instance


def wrapped(instance, pawn, now, flagged_targets):
    event = original(instance, pawn, now, flagged_targets)
    global _dumps
    try:
        with _lock:
            room = _dumps < MAX_DUMPS
            if room:
                _dumps += 1
        if room:
            dump(instance, event)
    except Exception as ex:  # the probe may never break the mod's hot path
        say(f"   dump failed: {type(ex).__name__}: {ex}")
    return event


poller._read_instance = wrapped
say(f"{time.strftime('%H:%M:%S')} damage instance fields; wrapping _read_instance for {WINDOW_S:.0f} s, {MAX_DUMPS} hits at most")
say("   throw a grenade and let it burn, then use the action skill, then shoot")


def finish():
    time.sleep(WINDOW_S)
    poller._read_instance = original
    say(f"   unwrapped after {_dumps} dump(s)")
    say("done")


threading.Thread(target=finish, name="fields86", daemon=True).start()
