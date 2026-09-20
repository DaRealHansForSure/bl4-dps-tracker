"""
Phase 0 probe #9: does GetSummary_InventoryItemSelectionData name the item?

`InventoryItemSelectionData` is `{Item: GbxItem, Criteria: ...}` and the held weapon carries
its `GbxItem` in `weapon.Item`. Build the selection struct from it and ask the library for
its summary string, for every equipped weapon.

    pyexec probe_itemsummary.py

Prints to the console and to probe_output.txt.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import get_pc
from unrealsdk import logging
from unrealsdk.unreal import UObject

OUT = Path(__file__).parent / "probe_output.txt"
MAX_VALUE_CHARS = 600


def say(msg: str) -> None:
    logging.info(f"[dmgitemsummary] {msg}")
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


def struct_fields(struct_name: str) -> list[tuple[str, str, str]]:
    """(field name, property class, struct path or '') for each field of the named struct."""
    fields: list[tuple[str, str, str]] = []
    for struct in unrealsdk.find_all("ScriptStruct", exact=True):
        if str(struct.Name) != struct_name:
            continue
        for prop in struct._properties():
            inner = ""
            if str(prop.Class.Name) == "StructProperty":
                inner = str(prop.Struct._path_name())
            fields.append((str(prop.Name), str(prop.Class.Name), inner))
        return fields
    return fields


def dump_struct_definition(struct_name: str) -> None:
    say(f"   {struct_name}:")
    for field_name, prop_class, inner in struct_fields(struct_name):
        say(f"      {field_name}: {prop_class} {inner}")


def build_def_provider(weapon: UObject) -> Any:
    """The selection struct wants an InventoryItemDefProvider, not the GbxItem itself. Fill
    whichever of its fields matches a value we hold on the weapon."""
    kwargs: dict[str, Any] = {}
    for field_name, prop_class, inner in struct_fields("InventoryItemDefProvider"):
        if inner.endswith(".GbxItem"):
            kwargs[field_name] = weapon.Item
        elif inner.endswith(".GbxItemData"):
            kwargs[field_name] = weapon.Item.data
        elif inner.endswith(".InventoryIdentity"):
            kwargs[field_name] = weapon.Item.data.Identity
        elif prop_class in ("ObjectProperty", "InterfaceProperty", "WeakObjectProperty"):
            kwargs[field_name] = weapon
    say(f"      provider fields filled: {', '.join(kwargs) or 'none'}")
    return unrealsdk.make_struct("InventoryItemDefProvider", **kwargs)


def summarize(weapon: UObject, library: Any) -> str:
    try:
        provider = build_def_provider(weapon)
        selection = unrealsdk.make_struct("InventoryItemSelectionData", Item=provider)
    except Exception as ex:  # noqa: BLE001
        return f"<build failed {type(ex).__name__}: {ex}>"
    return described(lambda: library.GetSummary_InventoryItemSelectionData(selection))


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} item summary probe")
pawn = get_pc().Pawn
if pawn is None:
    say("   no pawn, load into the world first")
else:
    dump_struct_definition("InventoryItemSelectionData")
    dump_struct_definition("InventoryItemDefProvider")
    library = unrealsdk.find_class("InventoryItemBlueprintLibrary").ClassDefaultObject
    for index, slot in enumerate(pawn.EquippedInventorySlots.items):
        item = slot.InstancedInventory
        if item is None or str(item.Class.Name) != "OakWeapon":
            continue
        say(f"   slot {index}: {item._path_name()}")
        say(f"      BodyData: {described(lambda: item.BodyData._path_name())}")
        say(f"      InstanceId: {described(lambda: item.Item.data.InstanceId)}")
        say(f"      summary: {summarize(item, library)}")
    say("done")
