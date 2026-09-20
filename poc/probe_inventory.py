"""
Phase 0 probe #5: find where the equipped item's name lives.

probe_weapon.py showed that the OakWeapon actor carries no display name: its `Item` struct
is `{InstanceId, Identity: {}}` and `EquippedInventorySlots.items[i].SourceItemHandle` is a
bare handle. So the item record is held elsewhere and referenced by handle. Run this in the
world with weapons equipped:

    pyexec probe_inventory.py

Prints, to the console and to probe_output.txt:
  1. inventory-related property names on the pawn and the player controller, with the values
     of the object-typed ones (a component or manager is what we are looking for)
  2. the pawn's component list, by class
  3. every UFunction in the whole process whose name pairs Item/Inventory/Weapon/Gear with
     Name/Title/Display, with its owning class and signature
  4. class names that mention InventoryManager, ItemName, Naming or ItemData

Narrow on purpose: property names are read from the class, values only for a handful of
object properties. Walking every property value of the pawn froze the game before.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import get_pc
from unrealsdk import logging
from unrealsdk.unreal import UObject

OUT = Path(__file__).parent / "probe_output.txt"
MAX_VALUE_CHARS = 200
MAX_FUNCTION_LINES = 150
MAX_CLASS_LINES = 80

INVENTORY_PROP_PATTERN = re.compile(r"Inventory|Item|Gear|Loadout|Equip|Weapon", re.IGNORECASE)
NAME_FUNCTION_PATTERN = re.compile(
    r"(Item|Inventory|Weapon|Gear).*(Name|Title|Display)|(Name|Title|Display).*(Item|Inventory|Weapon|Gear)"
)
CLASS_NAME_PATTERN = re.compile(r"InventoryManager|ItemName|Naming|ItemData|ItemInfo|ItemRegistry", re.IGNORECASE)
OBJECT_PROPERTY_CLASSES = ("ObjectProperty", "InterfaceProperty", "WeakObjectProperty")


def say(msg: str) -> None:
    logging.info(f"[dmginv] {msg}")
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


def dump_inventory_props(label: str, obj: UObject) -> None:
    """Print the inventory-related property names of obj's class, reading values only for
    the object-typed ones."""
    say(f"   {label}: {obj._path_name()} ({obj.Class.Name})")
    for prop in obj.Class._properties():
        prop_name = str(prop.Name)
        if not INVENTORY_PROP_PATTERN.search(prop_name):
            continue
        prop_class = str(prop.Class.Name)
        if prop_class in OBJECT_PROPERTY_CLASSES:
            say(f"      .{prop_name}: {prop_class} = {described(lambda: getattr(obj, prop_name))}")
        else:
            say(f"      .{prop_name}: {prop_class}")


def dump_components(pawn: UObject) -> None:
    for array_name in ("BlueprintCreatedComponents", "InstanceComponents"):
        try:
            components = getattr(pawn, array_name)
        except Exception as ex:  # noqa: BLE001
            say(f"   {array_name}: {type(ex).__name__}: {ex}")
            continue
        say(f"   {array_name}: {len(components)} element(s)")
        for component in components:
            say(f"      {described(lambda: component)}")


def function_signature(func: Any) -> str:
    return ", ".join(f"{p.Name}:{p.Class.Name}" for p in func._properties())


def dump_name_functions() -> None:
    printed = 0
    for func in unrealsdk.find_all("Function", exact=True):
        func_name = str(func.Name)
        if not NAME_FUNCTION_PATTERN.search(func_name):
            continue
        printed += 1
        if printed > MAX_FUNCTION_LINES:
            say(f"   ... more than {MAX_FUNCTION_LINES} matches, stopping")
            return
        say(f"   {described(lambda: func.Outer._path_name())}.{func_name}({function_signature(func)})")
    say(f"   {printed} matching function(s)")


def dump_matching_classes() -> None:
    printed = 0
    for cls in unrealsdk.find_all("Class", exact=True):
        class_name = str(cls.Name)
        if not CLASS_NAME_PATTERN.search(class_name):
            continue
        printed += 1
        if printed > MAX_CLASS_LINES:
            say(f"   ... more than {MAX_CLASS_LINES} matches, stopping")
            return
        say(f"   {described(lambda: cls._path_name())}")
    say(f"   {printed} matching class(es)")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} inventory probe")
pc = get_pc()
pawn = pc.Pawn
if pawn is None:
    say("   no pawn, load into the world first")
else:
    say("step 1/4: inventory-related properties")
    dump_inventory_props("pawn", pawn)
    dump_inventory_props("controller", pc)

    say("step 2/4: pawn components")
    dump_components(pawn)

    say("step 3/4: name-related functions anywhere")
    dump_name_functions()

    say("step 4/4: item-related classes")
    dump_matching_classes()
    say("done")
