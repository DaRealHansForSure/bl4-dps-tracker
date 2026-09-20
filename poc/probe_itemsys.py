"""
Phase 0 probe #6: learn the vocabulary of BL4's item system.

probe_inventory.py found no class or function using the BL3-era names (InventoryManager,
ItemData, ...). The weapon's `Item.data.Identity` struct and the controller's
`CachedGbxInventoryChannel` property both have types whose path names the module the item
system lives in. This probe reads those paths, then lists everything in that module.

    pyexec probe_itemsys.py

Prints, to the console and to probe_output.txt:
  1. the struct/class types behind the item handle fields on the held weapon and the slots
  2. every `/Script/*` package whose name mentions Gbx, Oak, Nexus, Inventory or Item
  3. for each module found in step 1 (plus any package with Inventory/Item in its name):
     all class and struct names, and every function whose name suggests a text accessor

Names only, except for the handful of handle fields in step 1.
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
MAX_LINES_PER_LIST = 200

PACKAGE_PATTERN = re.compile(r"Gbx|Oak|Nexus|Inventory|Item", re.IGNORECASE)
MODULE_OF_INTEREST_PATTERN = re.compile(r"Inventory|Item", re.IGNORECASE)
TEXT_FUNCTION_PATTERN = re.compile(r"Name|Text|Title|Display|String|Label", re.IGNORECASE)


def say(msg: str) -> None:
    logging.info(f"[dmgitemsys] {msg}")
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


def module_of(path_name: str) -> str | None:
    """'/Script/GbxInventory.SomeStruct' -> 'GbxInventory'."""
    match = re.match(r"^/Script/([^.]+)\.", path_name)
    if match is None:
        return None
    return match.group(1)


def struct_path(value: Any) -> str:
    return str(value._type._path_name())


def property_class_path(obj: UObject, prop_name: str) -> str:
    """Path of the class an ObjectProperty points to, read from the property definition."""
    for prop in obj.Class._properties():
        if str(prop.Name) == prop_name:
            return str(prop.PropertyClass._path_name())
    return "<property not found>"


def dump_handle_types(pc: UObject, pawn: UObject) -> set[str]:
    modules: set[str] = set()

    def note(label: str, fn) -> None:
        path = described(fn)
        say(f"   {label}: {path}")
        module = module_of(path.strip("'"))
        if module is not None:
            modules.add(module)

    held = pawn.ActiveWeapons.Slots[0].Weapon
    note("held weapon class", lambda: held.Class._path_name())
    note("Item struct", lambda: struct_path(held.Item))
    note("Item.data struct", lambda: struct_path(held.Item.data))
    note("Item.data.Identity struct", lambda: struct_path(held.Item.data.Identity))
    note("Item.State struct", lambda: struct_path(held.Item.State))
    note("slot struct", lambda: struct_path(pawn.EquippedInventorySlots.items[0]))
    note("slot SourceItemHandle struct", lambda: struct_path(pawn.EquippedInventorySlots.items[0].SourceItemHandle))
    note("controller CachedGbxInventoryChannel type", lambda: property_class_path(pc, "CachedGbxInventoryChannel"))
    note("controller CachedInventoryChannel type", lambda: property_class_path(pc, "CachedInventoryChannel"))
    note("pawn InventoryAugmentManager struct", lambda: struct_path(pawn.InventoryAugmentManager))
    return modules


def dump_script_packages() -> set[str]:
    """List the native modules and return the ones whose name says inventory or item."""
    interesting: set[str] = set()
    names = []
    for package in unrealsdk.find_all("Package", exact=True):
        name = str(package.Name)
        if not name.startswith("/Script/"):
            continue
        module = name[len("/Script/"):]
        if PACKAGE_PATTERN.search(module):
            names.append(module)
        if MODULE_OF_INTEREST_PATTERN.search(module):
            interesting.add(module)
    say(f"   {', '.join(sorted(names))}")
    return interesting


def dump_module_types(module: str) -> None:
    class_names = []
    struct_names = []
    for cls in unrealsdk.find_all("Class", exact=True):
        if str(cls.Outer.Name) == f"/Script/{module}":
            class_names.append(str(cls.Name))
    for struct in unrealsdk.find_all("ScriptStruct", exact=True):
        if str(struct.Outer.Name) == f"/Script/{module}":
            struct_names.append(str(struct.Name))
    say(f"   {module}: {len(class_names)} classes: {', '.join(sorted(class_names))[:4000]}")
    say(f"   {module}: {len(struct_names)} structs: {', '.join(sorted(struct_names))[:4000]}")


def dump_module_text_functions(module: str) -> None:
    printed = 0
    for func in unrealsdk.find_all("Function", exact=True):
        if not TEXT_FUNCTION_PATTERN.search(str(func.Name)):
            continue
        owner = func.Outer
        if owner is None or str(owner.Outer.Name) != f"/Script/{module}":
            continue
        printed += 1
        if printed > MAX_LINES_PER_LIST:
            say(f"      ... more than {MAX_LINES_PER_LIST} matches, stopping")
            return
        signature = ", ".join(f"{p.Name}:{p.Class.Name}" for p in func._properties())
        say(f"      {owner.Name}.{func.Name}({signature})")
    say(f"   {module}: {printed} text-like function(s)")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} item system probe")
pc = get_pc()
pawn = pc.Pawn
if pawn is None:
    say("   no pawn, load into the world first")
else:
    say("step 1/3: types behind the item handle fields")
    modules = dump_handle_types(pc, pawn)

    say("step 2/3: native modules mentioning Gbx/Oak/Nexus/Inventory/Item")
    modules |= dump_script_packages()
    modules.discard("CoreUObject")
    modules.discard("Engine")
    say(f"   modules to enumerate: {', '.join(sorted(modules))}")

    say("step 3/3: classes, structs and text-like functions per module")
    for module in sorted(modules):
        dump_module_types(module)
        dump_module_text_functions(module)
    say("done")
