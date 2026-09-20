"""
Phase 0 probe #7: find the item display name inside BL4's GbxItem system or its HUD.

probe_itemsys.py established the vocabulary: `GbxGame.GbxItem` / `GbxItemData` /
`InventoryIdentity` / `InventoryItemHandle`, and `OakUIDataCollector_*` classes feeding the
HUD. This probe reads the definitions of those structs, lists every Item/Inventory/
DataCollector class in the process, every function on Item/Inventory classes, and the
property lists of the weapon-related data collectors.

    pyexec probe_itemnames.py

Names and definitions only; no object values are read, so this is cheap and safe.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import unrealsdk
from unrealsdk import logging

OUT = Path(__file__).parent / "probe_output.txt"
MAX_FUNCTION_LINES = 400
NAMES_PER_LINE = 25

ITEM_STRUCTS = (
    "/Script/GbxGame.GbxItem",
    "/Script/GbxGame.GbxItemData",
    "/Script/GbxGame.InventoryIdentity",
    "/Script/GbxGame.GbxItemState",
    "/Script/GbxGame.InventoryItemHandle",
)
CLASS_PATTERN = re.compile(r"Item|Inventory|DataCollector|Naming|Gestalt", re.IGNORECASE)
FUNCTION_OWNER_PATTERN = re.compile(r"Item|Inventory", re.IGNORECASE)
COLLECTOR_PATTERN = re.compile(r"DataCollector.*(Weapon|Item|Equip|Ammo|Gear|Card|Slot|Loadout|HUD)", re.IGNORECASE)


def say(msg: str) -> None:
    logging.info(f"[dmgitemnames] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def say_names(indent: str, names: list[str]) -> None:
    """Print a long name list a few per line, so nothing gets cut off."""
    for start in range(0, len(names), NAMES_PER_LINE):
        say(f"{indent}{', '.join(names[start:start + NAMES_PER_LINE])}")


def property_line(prop: Any) -> str:
    prop_class = str(prop.Class.Name)
    detail = ""
    try:
        if prop_class == "StructProperty":
            detail = f" -> {prop.Struct._path_name()}"
        elif prop_class in ("ObjectProperty", "WeakObjectProperty", "SoftObjectProperty", "InterfaceProperty"):
            detail = f" -> {prop.PropertyClass._path_name()}"
        elif prop_class == "ArrayProperty":
            detail = f" of {prop.Inner.Class.Name}"
    except Exception as ex:  # noqa: BLE001
        detail = f" <err {type(ex).__name__}: {ex}>"
    return f"{prop.Name}: {prop_class}{detail}"


def dump_struct_definition(path: str) -> None:
    struct = unrealsdk.find_object("ScriptStruct", path)
    if struct is None:
        say(f"   {path}: not found")
        return
    say(f"   {path} (size {struct.PropertySize})")
    for prop in struct._properties():
        say(f"      {property_line(prop)}")


def dump_matching_classes() -> None:
    by_module: dict[str, list[str]] = {}
    for cls in unrealsdk.find_all("Class", exact=True):
        name = str(cls.Name)
        if not CLASS_PATTERN.search(name):
            continue
        module = str(cls.Outer.Name)
        by_module.setdefault(module, []).append(name)
    for module in sorted(by_module):
        names = sorted(by_module[module])
        say(f"   {module}: {len(names)}")
        say_names("      ", names)


def dump_item_class_functions() -> None:
    printed = 0
    for func in unrealsdk.find_all("Function", exact=True):
        owner = func.Outer
        if owner is None or not FUNCTION_OWNER_PATTERN.search(str(owner.Name)):
            continue
        printed += 1
        if printed > MAX_FUNCTION_LINES:
            say(f"   ... more than {MAX_FUNCTION_LINES} matches, stopping")
            return
        signature = ", ".join(f"{p.Name}:{p.Class.Name}" for p in func._properties())
        say(f"   {owner.Name}.{func.Name}({signature})")
    say(f"   {printed} function(s) on Item/Inventory classes")


def dump_collector_properties() -> None:
    for cls in unrealsdk.find_all("Class", exact=True):
        name = str(cls.Name)
        if not COLLECTOR_PATTERN.search(name):
            continue
        say(f"   {cls._path_name()}")
        for prop in cls._properties():
            say(f"      {property_line(prop)}")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} item names probe")

say("step 1/4: item struct definitions")
for struct_path in ITEM_STRUCTS:
    dump_struct_definition(struct_path)

say("step 2/4: Item/Inventory/DataCollector classes by module")
dump_matching_classes()

say("step 3/4: functions on Item/Inventory classes")
dump_item_class_functions()

say("step 4/4: weapon-related data collector properties")
dump_collector_properties()
say("done")
