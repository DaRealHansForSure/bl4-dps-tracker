"""
Phase 0 probe #20: from the held weapon to its part definitions.

probe_names4 showed the name machinery as reflected Nexus definitions: `InventoryTypeDef`
(ItemBaseName, NamingStrategyDef), `InventoryPartDef` (Aspects, GestaltPartNames), the
`InventoryNamingAspect` (PrefixPartList/TitlePartList/SuffixPartList of def pointers) and
`InventoryNamePartDef` (PartName: Text). The SDK can read any such definition by name
(`FGbxDefPtr(name, type)`), so what is missing is the list of the gun's parts. This probe
looks for it on the live weapon:

1. every component of the weapon actor, with the values of its part-, gestalt-, mesh-,
   serial- and def-like properties (the mesh keeps the active gestalt part names);
2. every function of the weapon's class chain and of the actor-part statics and interfaces;
3. the definitions of the structs around parts and serial numbers;
4. a check whether the two pointers at the start of `Item.data.Identity` are resolved
   definitions: their addresses against the address of a known def pointer.

    pyexec probe_names5.py    with the gun in hand

No hooks; only object reads and two no-argument getters.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import get_pc
from unrealsdk import logging
from unrealsdk.unreal import FGbxDefPtr, UFunction, UObject, WrappedArray, WrappedStruct

OUT = Path(__file__).parent / "probe_output.txt"
INTERESTING = re.compile(r"Part|Gestalt|Mesh|Serial|Def|Identity|Item|Name|Body|Manufacturer|Rarity|Level|Type", re.IGNORECASE)
PART_CLASSES = ("GbxActorPartStatics", "GbxActorPartOwner", "GbxActorPartSelectionProvider", "GbxActorPartSerialNumberProvider", "ExternalGestaltPartInterface", "GestaltPartData", "GbxItemContainerOwner", "OakInventoryStatics", "InventoryItemBlueprintLibrary", "GbxEquippedInventorySlotOwner")
STRUCTS = ("/Script/GbxGame.GbxSerialNumberIndex", "/Script/GbxGame.InventoryItemSelectionData", "/Script/GbxGame.InventoryItemHandle", "/Script/GbxGame.GbxItem", "/Script/GbxGame.GbxItemData", "/Script/GbxGame.InventoryPartDef", "/Script/GbxGame.InventoryTypeDef", "/Script/GbxGame.InventoryCompDef", "/Script/GbxGame.InventoryNamingAspect", "/Script/GbxGame.InventoryNamePartDef", "/Script/GbxGame.InventoryNameStrategyDef", "/Script/GbxGame.InventoryTags")
KNOWN_DEF = ("def_menu_equip", "GbxUIWidgetDef")
MAX_ITEMS = 16
MAX_VALUE_CHARS = 400


def say(msg: str) -> None:
    logging.info(f"[dmgnames5] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def type_text(prop: Any) -> str:
    kind = str(prop.Class.Name).replace("Property", "")
    try:
        if kind == "Struct":
            return f"Struct<{prop.Struct.Name}>"
        if kind == "Object":
            return f"Object<{prop.PropertyClass.Name}>"
        if kind == "Array":
            return f"Array<{type_text(prop.Inner)}>"
    except Exception:  # noqa: BLE001
        pass
    return kind


def described(value: Any, depth: int = 0) -> str:
    try:
        if isinstance(value, UObject):
            return f"{value.Class.Name}'{value._path_name()}'"
        if isinstance(value, WrappedStruct):
            if depth >= 2:
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


def signature(func: UFunction) -> str:
    return ", ".join(f"{prop.Name}:{type_text(prop)}" for prop in func._properties())


def class_chain(obj: UObject) -> list[Any]:
    chain = []
    cls = obj.Class
    while cls is not None:
        chain.append(cls)
        cls = cls.SuperField
    return chain


def dump_interesting_values(label: str, obj: UObject) -> None:
    for prop in obj.Class._properties():
        name = str(prop.Name)
        kind = str(prop.Class.Name)
        if "Delegate" in kind or not INTERESTING.search(name):
            continue
        try:
            value = getattr(obj, name)
        except Exception as ex:  # noqa: BLE001
            say(f"      {label}.{name}: <err {type(ex).__name__}>")
            continue
        text = described(value)
        if text in ("None", "[]", "''", "0", "0.0", "False"):
            continue
        say(f"      {label}.{name} ({type_text(prop)}) = {text}")


def held_weapon() -> UObject | None:
    pc = get_pc()
    pawn = pc.Pawn if pc is not None else None
    if pawn is None:
        return None
    try:
        return pawn.ActiveWeapons.Slots[0].Weapon
    except Exception:  # noqa: BLE001
        return None


def dump_components(weapon: UObject) -> None:
    say("step 1/4: the weapon and its components, part-like values")
    say(f"   weapon {weapon._path_name()} ({' -> '.join(str(c.Name) for c in class_chain(weapon))})")
    dump_interesting_values("weapon", weapon)
    try:
        components = list(weapon.K2_GetComponentsByClass(unrealsdk.find_class("ActorComponent")))
    except Exception as ex:  # noqa: BLE001
        say(f"   K2_GetComponentsByClass failed: {type(ex).__name__}: {ex}")
        components = []
    say(f"   {len(components)} component(s)")
    for component in components:
        say(f"   component {component._path_name()} ({' -> '.join(str(c.Name) for c in class_chain(component)[:3])})")
        dump_interesting_values("   ", component)


def dump_part_functions(weapon: UObject) -> None:
    say("step 2/4: functions on the weapon's class chain and the part classes")
    seen: set[str] = set()
    for cls in class_chain(weapon):
        for field in cls._fields():
            if isinstance(field, UFunction) and str(field.Name) not in seen:
                seen.add(str(field.Name))
                if INTERESTING.search(str(field.Name)):
                    say(f"   {cls.Name}.{field.Name}({signature(field)})")
    for name in PART_CLASSES:
        try:
            cls = unrealsdk.find_class(name)
        except ValueError:
            say(f"   {name}: not found")
            continue
        functions = [field for field in cls._fields() if isinstance(field, UFunction)]
        say(f"   {cls._path_name()} ({len(functions)} fn):")
        for func in functions:
            say(f"      {func.Name}({signature(func)})")


def dump_structs() -> None:
    say("step 3/4: struct definitions around parts and serials")
    for path in STRUCTS:
        try:
            struct = unrealsdk.find_object("ScriptStruct", path)
        except ValueError:
            say(f"   {path}: not found")
            continue
        say(f"   {path} ({struct._get_struct_size()} bytes): " + ", ".join(f"{prop.Name}:{type_text(prop)}" for prop in struct._properties()))


def check_identity_pointers(weapon: UObject) -> None:
    say("step 4/4: are the identity's first two pointers resolved definitions?")
    try:
        known = FGbxDefPtr(*KNOWN_DEF)
        known_address = int(known._get_address())
        say(f"   known def {KNOWN_DEF} resolved at {known_address:#x}: {known!r}"[:400])
    except Exception as ex:  # noqa: BLE001
        say(f"   known def failed: {type(ex).__name__}: {ex}")
        known_address = 0
    try:
        identity = weapon.Item.data.Identity
        address = int(identity._get_address())
        import ctypes
        raw = ctypes.string_at(address, 16)
        first = int.from_bytes(raw[0:8], "little")
        second = int.from_bytes(raw[8:16], "little")
        say(f"   identity pointers: {first:#x}, {second:#x}; known def at {known_address:#x}; distance {first - known_address:#x}")
    except Exception as ex:  # noqa: BLE001
        say(f"   identity read failed: {type(ex).__name__}: {ex}")
    for name, kind in (("Body_JAK_SG", "InventoryTypeDef"), ("JAK_SG", "InventoryTypeDef"), ("Shotgun", "InventoryTypeDef"), ("SG_JAK", "InventoryCompDef")):
        try:
            pointer = FGbxDefPtr(name, kind)
            say(f"   FGbxDefPtr({name!r}, {kind!r}) -> {pointer!r}"[:600])
        except Exception as ex:  # noqa: BLE001
            say(f"   FGbxDefPtr({name!r}, {kind!r}) failed: {type(ex).__name__}: {ex}")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v5")
weapon = held_weapon()
if weapon is None:
    say("no weapon in hand")
else:
    for step in (dump_components, dump_part_functions, check_identity_pointers):
        try:
            step(weapon)
        except Exception as ex:  # noqa: BLE001
            say(f"   step failed: {type(ex).__name__}: {ex}")
    try:
        dump_structs()
    except Exception as ex:  # noqa: BLE001
        say(f"   structs failed: {type(ex).__name__}: {ex}")
say("done")
