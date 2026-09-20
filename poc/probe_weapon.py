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
  4. for the weapon in hand: the `Item` struct, the `BodyData` object, and every
     zero-argument getter in the class chain whose name looks like a name accessor,
     called and printed (run 3 found no plain name property on the object itself)

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
GETTER_FRAGMENTS = ("Name", "Title", "Display", "Label")
MAX_STRUCT_DEPTH = 2


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


def dump_struct_fields(label: str, struct: Any, depth: int = 1) -> None:
    """Print every non-delegate field of one struct value, recursing into nested structs
    up to MAX_STRUCT_DEPTH levels."""
    for prop in struct._type._properties():
        prop_name = str(prop.Name)
        prop_class = str(prop.Class.Name)
        if "Delegate" in prop_class:
            say(f"      {label}.{prop_name}: {prop_class} (skipped)")
            continue
        say(f"      {label}.{prop_name}: {prop_class} = {described(lambda: getattr(struct, prop_name))}")
        if prop_class == "StructProperty" and depth < MAX_STRUCT_DEPTH:
            try:
                nested = getattr(struct, prop_name)
            except Exception:  # noqa: BLE001
                continue
            dump_struct_fields(f"{label}.{prop_name}", nested, depth + 1)


def dump_struct_array(label: str, array: Any) -> None:
    say(f"   {label}: {len(array)} element(s)")
    for index, element in enumerate(array):
        say(f"   [{index}] {described(lambda: element._type._path_name())}")
        dump_struct_fields(f"[{index}]", element)


def dump_weapon(index: int, weapon: UObject) -> None:
    say(f"   weapon {index}: {weapon._path_name()}")
    # _superfields() yields the class itself first, then each parent up to UObject.
    say(f"      class chain: {' -> '.join(str(c.Name) for c in weapon.Class._superfields())}")
    say(f"      repr: {described(lambda: repr(weapon))}")
    # _properties() follows the property link, so this already includes inherited properties.
    all_names = [str(p.Name) for p in weapon.Class._properties()]
    say(f"      props ({len(all_names)}): {', '.join(all_names)[:2000]}")
    name_like = [n for n in all_names if any(fragment in n for fragment in NAME_LIKE_FRAGMENTS)]
    say("      name-like values:")
    for prop_name in name_like:
        say(f"         .{prop_name} = {described(lambda: getattr(weapon, prop_name))}")


def dump_name_getters(obj: UObject) -> None:
    """List every zero-argument function in the class chain whose name looks like a name
    accessor, and call it. Getters like these only read state, so calling them is safe."""
    # _fields() already walks the parent classes, so one pass over the object's class is enough.
    for field in obj.Class._fields():
        if str(field.Class.Name) != "Function":
            continue
        func_name = str(field.Name)
        if not any(fragment in func_name for fragment in GETTER_FRAGMENTS):
            continue
        owner = str(field.Outer.Name)
        signature = ", ".join(f"{p.Name}:{p.Class.Name}" for p in field._properties())
        # NumParams counts the return value as a parameter, so a pure getter has exactly one.
        has_return = field._find_return_param() is not None
        input_count = field.NumParams - (1 if has_return else 0)
        if input_count > 0:
            say(f"      {owner}.{func_name}({signature}) needs arguments, not called")
            continue
        say(f"      {owner}.{func_name}({signature}) -> {described(lambda: getattr(obj, func_name)())}")


def dump_held_weapon(weapon: UObject) -> None:
    say(f"   held: {weapon._path_name()}")
    say("   Item struct:")
    try:
        dump_struct_fields("Item", weapon.Item)
    except Exception as ex:  # noqa: BLE001
        say(f"      Item: {type(ex).__name__}: {ex}")
    say("   BodyData object, name-like props:")
    try:
        body = weapon.BodyData
        say(f"      {described(lambda: body)}")
        for prop in body.Class._properties():
            prop_name = str(prop.Name)
            if any(fragment in prop_name for fragment in NAME_LIKE_FRAGMENTS):
                say(f"      .{prop_name} = {described(lambda: getattr(body, prop_name))}")
    except Exception as ex:  # noqa: BLE001
        say(f"      BodyData: {type(ex).__name__}: {ex}")
    say("   name-like getters in the class chain:")
    try:
        dump_name_getters(weapon)
    except Exception as ex:  # noqa: BLE001
        say(f"      getters: {type(ex).__name__}: {ex}")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} weapon probe")
pc = get_pc()
pawn = pc.Pawn
say(f"   pawn: {described(lambda: pawn._path_name())}")
if pawn is None:
    say("   no pawn, load into the world first")
else:
    say("step 1/4: ActiveWeapons.Slots")
    dump_struct_array("ActiveWeapons.Slots", pawn.ActiveWeapons.Slots)

    say("step 2/4: EquippedInventorySlots.items")
    dump_struct_array("EquippedInventorySlots.items", pawn.EquippedInventorySlots.items)

    say("step 3/4: weapon objects")
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

    say("step 4/4: the weapon in hand, in depth")
    held = None
    try:
        held = pawn.ActiveWeapons.Slots[0].Weapon
    except Exception as ex:  # noqa: BLE001
        say(f"   no active weapon: {type(ex).__name__}: {ex}")
    if held is not None:
        dump_held_weapon(held)
