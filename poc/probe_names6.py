"""
Phase 0 probe #21: resolve the gun's part definitions by name and read their name parts.

probe_names5 gave the missing link: the weapon's skeletal mesh carries the active gestalt
part names (`GestaltData.PartNames`, e.g. JAK_SG_Barrel_02), and an `InventoryPartDef` lists
its gestalt parts. A freshly constructed `FGbxDefPtr(name, type)` is unresolved (address 0)
until `_experimental_alloc_and_mem_leak()` resolves it from the store. This probe:

1. reads the mesh part names of the held weapon;
2. tries to resolve `InventoryPartDef`s under those names (and a few naming patterns), and
   for each resolved one prints its gestalt parts, its naming aspects and the texts of the
   name parts they list (prefixes, titles, suffixes) with their priorities;
3. tries to resolve the gun's `InventoryTypeDef` and `InventoryCompDef` under a few names
   and prints ItemBaseName, InventoryName, the type-level part lists and the naming
   strategy;
4. compares the addresses of the resolved definitions with the pointers inside the
   item identity (the two at the start and the elements of the first array), to learn
   whether the identity holds the very same definitions.

    pyexec probe_names6.py    with the gun in hand

No hooks. Resolving a definition leaks its memory (the SDK says so); a probe can afford it.
"""

from __future__ import annotations

import ctypes
import time
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import get_pc
from unrealsdk import logging
from unrealsdk.unreal import FGbxDefPtr, UObject, WrappedArray, WrappedStruct

OUT = Path(__file__).parent / "probe_output.txt"
PART_DEF = "InventoryPartDef"
TYPE_DEF = "InventoryTypeDef"
COMP_DEF = "InventoryCompDef"
NAME_PART_DEF = "InventoryNamePartDef"
MAX_ITEMS = 24
MAX_VALUE_CHARS = 500


def say(msg: str) -> None:
    logging.info(f"[dmgnames6] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def described(value: Any, depth: int = 0) -> str:
    try:
        if isinstance(value, UObject):
            return f"{value.Class.Name}'{value._path_name()}'"
        if isinstance(value, FGbxDefPtr):
            return repr(value)[:MAX_VALUE_CHARS]
        if isinstance(value, WrappedStruct):
            if depth >= 3:
                return f"<struct {value._type.Name}>"
            parts = []
            for prop in value._type._properties():
                name = str(prop.Name)
                try:
                    parts.append(f"{name}={described(getattr(value, name), depth + 1)}")
                except Exception as ex:  # noqa: BLE001
                    parts.append(f"{name}=<err {type(ex).__name__}>")
            return "{" + ", ".join(parts) + "}"
        if isinstance(value, WrappedArray):
            items = [described(item, depth + 1) for item in list(value)[:MAX_ITEMS]]
            more = "" if len(value) <= MAX_ITEMS else f", ... {len(value)} items"
            return "[" + ", ".join(items) + more + "]"
        text = repr(value)
        return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "..."
    except Exception as ex:  # noqa: BLE001
        return f"<err {type(ex).__name__}: {ex}>"


def resolve(name: str, kind: str) -> FGbxDefPtr | None:
    """A resolved def pointer, or None when the store has no such definition."""
    try:
        pointer = FGbxDefPtr(name, kind)
    except Exception as ex:  # noqa: BLE001
        say(f"   FGbxDefPtr({name!r}, {kind!r}) raised {type(ex).__name__}: {ex}")
        return None
    try:
        if int(pointer._get_address()) == 0:
            pointer._experimental_alloc_and_mem_leak()
    except Exception as ex:  # noqa: BLE001
        say(f"   resolving {name!r} ({kind}) raised {type(ex).__name__}: {ex}")
        return None
    try:
        if int(pointer._get_address()) == 0:
            return None
    except Exception:  # noqa: BLE001
        return None
    return pointer


def name_part_text(pointer: Any) -> str:
    """'PartName' (priority) of a name part def pointer, resolving it when needed."""
    try:
        if isinstance(pointer, FGbxDefPtr) and int(pointer._get_address()) == 0:
            pointer._experimental_alloc_and_mem_leak()
        return f"{pointer.PartName!r} (priority {pointer.priority}, stages {pointer.mingamestage}-{pointer.MaxGameStage})"
    except Exception as ex:  # noqa: BLE001
        return f"<{described(pointer)}: {type(ex).__name__}: {ex}>"


def dump_naming_aspects(label: str, aspects: Any) -> None:
    for index, aspect in enumerate(list(aspects)):
        try:
            type_name = str(aspect._type.Name)
        except Exception:  # noqa: BLE001
            type_name = "?"
        if "Naming" not in type_name:
            say(f"      {label} aspect {index}: {type_name}")
            continue
        say(f"      {label} aspect {index}: {type_name} row {aspect.DataTableRowName!r} prefixes off {aspect.bDisablePrefixes}")
        for list_name in ("PrefixPartList", "TitlePartList", "SuffixPartList"):
            try:
                entries = list(getattr(aspect, list_name))
            except Exception as ex:  # noqa: BLE001
                say(f"         {list_name}: <err {type(ex).__name__}: {ex}>")
                continue
            for entry in entries:
                say(f"         {list_name}: {name_part_text(entry)}")


def mesh_part_names(weapon: UObject) -> list[str]:
    names: list[str] = []
    for component in weapon.K2_GetComponentsByClass(unrealsdk.find_class("SkeletalMeshComponent")):
        try:
            for name in list(component.GestaltData.PartNames):
                if str(name) not in names:
                    names.append(str(name))
        except Exception:  # noqa: BLE001
            continue
    return names


def candidate_part_names(mesh_names: list[str]) -> list[str]:
    """Mesh part names and the variants a part definition might be called by."""
    candidates: list[str] = []
    for name in mesh_names:
        for candidate in (name, name.rsplit("_", 1)[0] if name.count("_") >= 3 else name):
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def dump_part_defs(weapon: UObject) -> list[tuple[str, FGbxDefPtr]]:
    say("step 1/3: part definitions by mesh part name")
    mesh_names = mesh_part_names(weapon)
    say(f"   mesh parts: {mesh_names}")
    resolved: list[tuple[str, FGbxDefPtr]] = []
    for name in candidate_part_names(mesh_names):
        pointer = resolve(name, PART_DEF)
        if pointer is None:
            say(f"   {name}: no {PART_DEF}")
            continue
        say(f"   {name}: {PART_DEF} at {int(pointer._get_address()):#x}")
        resolved.append((name, pointer))
        try:
            say(f"      gestalt parts {described(pointer.GestaltPartNames)}; description {pointer.DebugDisplayDescription!r}; serial {described(pointer.SerialIndex)}")
            dump_naming_aspects(name, pointer.Aspects)
        except Exception as ex:  # noqa: BLE001
            say(f"      fields: <err {type(ex).__name__}: {ex}>")
    return resolved


def dump_type_defs(weapon: UObject) -> list[tuple[str, FGbxDefPtr]]:
    say("step 2/3: type and composition definitions")
    body = str(weapon.BodyData._path_name()).rsplit(".", 1)[-1]  # Body_JAK_SG
    stem = body.replace("Body_", "")  # JAK_SG
    manufacturer, kind = stem.split("_", 1) if "_" in stem else (stem, "")
    names = [body, stem, f"{kind}_{manufacturer}", kind, kind.lower(), f"{manufacturer}_{kind}_Base", f"Base_{kind}", f"{kind}_{manufacturer}_Base"]
    resolved: list[tuple[str, FGbxDefPtr]] = []
    for kind_name in (TYPE_DEF, COMP_DEF):
        for name in names:
            pointer = resolve(name, kind_name)
            if pointer is None:
                continue
            say(f"   {name}: {kind_name} at {int(pointer._get_address()):#x}")
            resolved.append((name, pointer))
            for field in ("ItemBaseName", "InventoryName", "bInventoryNameIsFullName", "MaxNumPrefixes", "MaxNumSuffixes", "bInheritBasePartLists", "NamingStrategyDef", "BaseType", "BaseComposition", "Manufacturer", "Rarity"):
                try:
                    say(f"      {field} = {described(getattr(pointer, field))}")
                except Exception as ex:  # noqa: BLE001
                    say(f"      {field}: <err {type(ex).__name__}>")
            for list_name in ("PrefixPartList", "TitlePartList", "SuffixPartList"):
                try:
                    for entry in list(getattr(pointer, list_name)):
                        say(f"      {list_name}: {name_part_text(entry)}")
                except Exception as ex:  # noqa: BLE001
                    say(f"      {list_name}: <err {type(ex).__name__}>")
            try:
                dump_naming_aspects(name, pointer.Aspects)
            except Exception:  # noqa: BLE001
                pass
    if not resolved:
        say("   nothing resolved under those names")
    return resolved


def dump_part_values(weapon: UObject) -> None:
    """OakWeapon.GetPartValue(type) per value of its enum: whatever it returns per part type."""
    say("step 2b: OakWeapon.GetPartValue per part type")
    try:
        func = weapon.Class._find("GetPartValue")
        enum_prop = next(prop for prop in func._properties() if str(prop.Name) == "type")
        py_enum = enum_prop.Enum._as_py()
        for member in py_enum:
            try:
                say(f"   GetPartValue({member.name} = {int(member)}) -> {weapon.GetPartValue(member)}")
            except Exception as ex:  # noqa: BLE001
                say(f"   GetPartValue({member.name}): <err {type(ex).__name__}: {ex}>")
    except Exception as ex:  # noqa: BLE001
        say(f"   GetPartValue not usable: {type(ex).__name__}: {ex}")


def compare_with_identity(weapon: UObject, resolved: list[tuple[str, FGbxDefPtr]]) -> None:
    say("step 3/3: identity pointers against the resolved definitions")
    identity = weapon.Item.data.Identity
    address = int(identity._get_address())
    raw = ctypes.string_at(address, 216)
    pointers = [int.from_bytes(raw[0:8], "little"), int.from_bytes(raw[8:16], "little")]
    first_array = int.from_bytes(raw[16:24], "little")
    count = int.from_bytes(raw[24:28], "little")
    say(f"   identity: heads {pointers[0]:#x} {pointers[1]:#x}; first array {first_array:#x} x {count}")
    elements: list[int] = []
    if 0 < count <= 64 and first_array > 0x10000:
        try:
            data = ctypes.string_at(first_array, count * 8)
            elements = [int.from_bytes(data[i:i + 8], "little") for i in range(0, len(data), 8)]
            say(f"   first array words: {[f'{e:#x}' for e in elements]}")
        except Exception as ex:  # noqa: BLE001
            say(f"   first array unreadable: {type(ex).__name__}")
    known = {int(pointer._get_address()): name for name, pointer in resolved}
    for word in pointers + elements:
        if word in known:
            say(f"   {word:#x} is the resolved definition {known[word]!r}")
    say(f"   resolved definitions live at: {[f'{a:#x}' for a in known]}")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v6")
pc = get_pc()
pawn = pc.Pawn if pc is not None else None
weapon = None
try:
    weapon = pawn.ActiveWeapons.Slots[0].Weapon if pawn is not None else None
except Exception:  # noqa: BLE001
    weapon = None
if weapon is None:
    say("no weapon in hand")
else:
    resolved_defs: list[tuple[str, FGbxDefPtr]] = []
    for step in (dump_part_defs, dump_type_defs):
        try:
            resolved_defs.extend(step(weapon))
        except Exception as ex:  # noqa: BLE001
            say(f"   step failed: {type(ex).__name__}: {ex}")
    try:
        dump_part_values(weapon)
    except Exception as ex:  # noqa: BLE001
        say(f"   part values failed: {type(ex).__name__}: {ex}")
    try:
        compare_with_identity(weapon, resolved_defs)
    except Exception as ex:  # noqa: BLE001
        say(f"   compare failed: {type(ex).__name__}: {ex}")
say("done")
