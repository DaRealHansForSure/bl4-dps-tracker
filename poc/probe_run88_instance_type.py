"""What a damage instance and its damage type actually expose.

Probe 87 listed no fields of `RecentPlayerCausedDamageInstance` (the introspection route
was wrong) and found the damage type to be an `OakDamageTypeDef`, a Nexus data definition
rather than a UObject. This tries every introspection route on a ring element read straight
from the controller (no hook, no wrapper, so it runs next to probe 87) and prints:

  - the names of the struct's fields, by whichever route works;
  - the value of each field of a live element;
  - the same for the damage type definition, whose fields would be the way to tell a
    grenade's corrosive area from a skill's corrosive damage without any window guessing.
"""
import time
from pathlib import Path
import unrealsdk
from unrealsdk import logging
from mods_base import get_pc

OUT = Path(__file__).parent / "probe_output.txt"
RING_PROPERTY = "RecentlyCausedDamageInstances"
STRUCT_NAME = "RecentPlayerCausedDamageInstance"
MAX_VALUE_CHARS = 100


def say(m):
    logging.info("[type88] " + m)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(m + "\n")


def text(value):
    try:
        return repr(value)[:MAX_VALUE_CHARS]
    except Exception as ex:
        return f"<{type(ex).__name__}>"


def names_by_fields(owner):
    return [str(field.Name) for field in owner._fields()]


def names_by_properties(owner):
    return [str(field.Name) for field in owner._properties()]


def routes(value):
    """Every way of asking what a wrapped struct or object holds, and what each returns."""
    found = {}
    for name, call in (
        ("dir()", lambda: [n for n in dir(value) if not n.startswith("__")]),
        ("_type._fields()", lambda: names_by_fields(value._type)),
        ("_type._properties()", lambda: names_by_properties(value._type)),
        ("_get_field_names()", lambda: list(value._get_field_names())),
        ("Class._fields()", lambda: names_by_fields(value.Class)),
        ("Class._properties()", lambda: names_by_properties(value.Class)),
        ("find_struct._fields()", lambda: names_by_fields(unrealsdk.find_struct(STRUCT_NAME))),
        ("find_struct._properties()", lambda: names_by_properties(unrealsdk.find_struct(STRUCT_NAME))),
    ):
        try:
            result = call()
            found[name] = result
            say(f"      {name:<28} {len(result)} name(s): {text(result)}")
        except Exception as ex:
            say(f"      {name:<28} <{type(ex).__name__}: {ex}>"[:200])
    return found


def dump_values(value, names):
    for name in sorted(set(names)):
        if name.startswith("_"):
            continue
        try:
            field = getattr(value, name)
        except Exception as ex:
            say(f"      {name:<28} <{type(ex).__name__}: {ex}>"[:180])
            continue
        if callable(field):
            continue
        extra = ""
        for detail in ("_path_name", "Name"):
            try:
                attribute = getattr(field, detail)
                extra = f" {attribute() if callable(attribute) else attribute}"
                break
            except Exception:
                continue
        say(f"      {name:<28} {text(field)}{extra}"[:200])


say(f"{time.strftime('%H:%M:%S')} what the damage instance exposes")
controller = get_pc()
ring = getattr(controller, RING_PROPERTY)
say(f"   ring of {len(ring)} element(s), element type {type(ring[0]).__name__}")
say("   introspection routes on the instance:")
found = routes(ring[0])
names = []
for key in ("find_struct._fields()", "_type._fields()", "dir()"):
    if found.get(key):
        names = found[key]
        say(f"   using {key}")
        break

# The element with the most damage is the most likely to be a real hit rather than a cleared slot.
best = max(range(len(ring)), key=lambda index: float(getattr(ring[index], "TotalDamage", 0.0) or 0.0))
say(f"   values of slot {best} (the largest TotalDamage in the ring):")
dump_values(ring[best], names or [n for n in dir(ring[best]) if not n.startswith("_")])

say("   the damage type of that slot:")
try:
    damage_type = ring[best].DamageType
    say(f"      type {type(damage_type).__name__}, name {getattr(damage_type, '_name', None)!r}")
    type_names = []
    for name, call in (
        ("dir()", lambda: [n for n in dir(damage_type) if not n.startswith("__")]),
        ("_fields()", lambda: names_by_fields(damage_type)),
        ("Class._fields()", lambda: names_by_fields(damage_type.Class)),
    ):
        try:
            result = call()
            type_names = type_names or result
            say(f"      {name:<28} {len(result)} name(s): {text(result)}")
        except Exception as ex:
            say(f"      {name:<28} <{type(ex).__name__}: {ex}>"[:200])
    dump_values(damage_type, type_names)
except Exception as ex:
    say(f"      unreadable: {type(ex).__name__}: {ex}")
say("done")
