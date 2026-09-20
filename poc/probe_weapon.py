"""
Phase 0 probe #4: map the equipped-weapon structs so the real mod can attribute damage.

The dmgpoc mod dumps `ActiveWeapons` and `EquippedInventorySlots` once when the controller
resolves, which turned out to be before any weapon is equipped (both arrays were empty). Run
this while standing in the world with weapons equipped:

    pyexec probe_weapon.py

Prints, to the console and to probe_output.txt:
  1. every element of `ActiveWeapons.Slots` and `EquippedInventorySlots.items`
  2. for the first two weapon objects: the class chain and every property name
  3. the values of the name-like properties, so we can pick the display-name source

Deliberately narrow: only the two arrays and the weapon objects are walked. Walking the
whole pawn froze the game in an earlier probe.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mods_base import get_pc
from unrealsdk import logging
from unrealsdk.unreal import UObject

OUT = Path(__file__).parent / "probe_output.txt"
MAX_WEAPONS_TO_DUMP = 2
MAX_VALUE_CHARS = 200
NAME_LIKE_FRAGMENTS = ("Name", "Balance", "Def", "Data", "Title", "Manufacturer", "Type", "Part")


def say(msg: str) -> None:
    logging.info(f"[dmgweapon] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def described(fn) -> str:
    """Run fn and return its result as a string, or the error text instead of raising."""
    try:
        value = fn()
    except Exception as ex:  # noqa: BLE001
        return f"<err {type(ex).__name__}: {ex}>"
    if isinstance(value, UObject):
        return f"{value._path_name()} ({value.Class.Name})"
    return repr(value)[:MAX_VALUE_CHARS]


def dump_struct_fields(label: str, struct: Any) -> None:
    """Print every non-delegate field of one struct value."""
    for prop in struct._type._properties():
        prop_name = str(prop.Name)
        if "Delegate" in str(prop.Class.Name):
            say(f"      {label}.{prop_name}: {prop.Class.Name} (skipped)")
            continue
        say(f"      {label}.{prop_name}: {prop.Class.Name} = {described(lambda: getattr(struct, prop_name))}")


def dump_struct_array(label: str, array: Any) -> None:
    say(f"   {label}: {len(array)} element(s)")
    for index, element in enumerate(array):
        say(f"   [{index}] {described(lambda: element._type._path_name())}")
        dump_struct_fields(f"[{index}]", element)


def class_chain(obj: UObject) -> list[Any]:
    chain = []
    cls = obj.Class
    while cls is not None:
        chain.append(cls)
        cls = cls._superfield
    return chain


def dump_weapon(index: int, weapon: UObject) -> None:
    say(f"   weapon {index}: {weapon._path_name()}")
    say(f"      class chain: {' -> '.join(str(c.Name) for c in class_chain(weapon))}")
    say(f"      repr: {described(lambda: repr(weapon))}")
    # _properties() follows the property link, so this already includes inherited properties.
    all_names = [str(p.Name) for p in weapon.Class._properties()]
    say(f"      props ({len(all_names)}): {', '.join(all_names)[:2000]}")
    name_like = [n for n in all_names if any(fragment in n for fragment in NAME_LIKE_FRAGMENTS)]
    say("      name-like values:")
    for prop_name in name_like:
        say(f"         .{prop_name} = {described(lambda: getattr(weapon, prop_name))}")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} weapon probe")
pc = get_pc()
pawn = pc.Pawn
say(f"   pawn: {described(lambda: pawn._path_name())}")
if pawn is None:
    say("   no pawn, load into the world first")
else:
    say("step 1/3: ActiveWeapons.Slots")
    dump_struct_array("ActiveWeapons.Slots", pawn.ActiveWeapons.Slots)

    say("step 2/3: EquippedInventorySlots.items")
    dump_struct_array("EquippedInventorySlots.items", pawn.EquippedInventorySlots.items)

    say("step 3/3: weapon objects")
    weapons_dumped = 0
    for item in pawn.EquippedInventorySlots.items:
        weapon = None
        try:
            weapon = item.InstancedInventory
        except Exception as ex:  # noqa: BLE001
            say(f"   item without InstancedInventory: {type(ex).__name__}: {ex}")
        if weapon is None:
            continue
        dump_weapon(weapons_dumped, weapon)
        weapons_dumped += 1
        if weapons_dumped >= MAX_WEAPONS_TO_DUMP:
            break
    say(f"done, {weapons_dumped} weapon(s) dumped")
