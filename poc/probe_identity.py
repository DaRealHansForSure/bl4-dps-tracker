"""
Phase 0 probe #16: is there a weapon identity that survives a restart?

`Item.data.InstanceId` is a process-wide counter, not an item property: the same four guns
had the ids 277/279/281/284 in every launch that continued with the same character and
557/559 in the one launch where another character had been loaded first (sessions of
2026-09-20, see findings.md). Per-weapon records keyed on it split on every character switch.

The candidate is `Item.data.Identity`, a 216-byte `InventoryIdentity` with no reflected
fields. This probe dumps its raw bytes for every equipped weapon. Run it twice: once now,
once after a launch in which the instance ids have shifted (start the game, load another
character, then switch to this one). `compare_identity.py` then shows which bytes agree
between the two dumps of the same gun and differ between guns; those are usable identity.

    pyexec probe_identity.py

Prints to the console and to probe_output.txt. The raw read goes through ctypes at an
address computed from the live weapon object and the reflected property offsets, and only
after the struct wrapper reports the same address.
"""

from __future__ import annotations

import ctypes
import os
import re
import time
from pathlib import Path
from typing import Any

from mods_base import get_pc
from unrealsdk import logging
from unrealsdk.unreal import UObject, WrappedArray, WrappedStruct

OUT = Path(__file__).parent / "probe_output.txt"
WEAPON_CLASS_NAME = "OakWeapon"
WORDS_PER_LINE = 4
WORD_BYTES = 8
MAX_DUMP_BYTES = 1024
MAX_VALUE_CHARS = 200
MAX_ARRAY_ITEMS = 6
# Heap and image addresses of a 64-bit Windows process; a word in this range is probably a pointer.
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
CANDIDATE_FIELD_PATTERN = re.compile(
    r"Serial|Guid|Seed|Part|Balance|Level|Rarity|Manufacturer|Identity|Unique|Hash|Name|Def\b", re.IGNORECASE
)

RUN_STAMP = f"{time.strftime('%Y%m%d-%H%M%S')}-pid{os.getpid()}"


def say(msg: str) -> None:
    logging.info(f"[dmgidentity] {msg}")
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
    if isinstance(value, WrappedArray):
        items = [described(lambda item=item: item) for item in list(value)[:MAX_ARRAY_ITEMS]]
        more = "" if len(value) <= MAX_ARRAY_ITEMS else f", ... ({len(value)} items)"
        return f"[{', '.join(items)}{more}]"
    return repr(value)[:MAX_VALUE_CHARS]


def body_stem(weapon: UObject) -> str:
    try:
        return str(weapon.BodyData._path_name()).rsplit(".", 1)[-1]
    except Exception:  # noqa: BLE001
        return "?"


def equipped_weapons(pawn: Any) -> list[UObject]:
    weapons: list[UObject] = []
    for slot in pawn.EquippedInventorySlots.items:
        item = slot.InstancedInventory
        if item is not None and str(item.Class.Name) == WEAPON_CLASS_NAME:
            weapons.append(item)
    return weapons


def struct_property(owner_struct: Any, name: str) -> Any:
    """The named StructProperty of a class or script struct, inherited ones included."""
    for prop in owner_struct._properties():
        if str(prop.Name) == name:
            return prop
    raise KeyError(name)


def dump_bytes(label: str, data: bytes, marks: dict[int, str]) -> None:
    """Hex words with offsets; `marks` names the offsets where a known field begins."""
    say(f"   {label}: {len(data)} bytes")
    for line_start in range(0, len(data), WORDS_PER_LINE * WORD_BYTES):
        words: list[str] = []
        for offset in range(line_start, min(line_start + WORDS_PER_LINE * WORD_BYTES, len(data)), WORD_BYTES):
            word = data[offset : offset + WORD_BYTES]
            note = marks.get(offset, "")
            value = int.from_bytes(word, "little") if len(word) == WORD_BYTES else 0
            if note:
                note = f"<{note}>"
            elif POINTER_MIN <= value <= POINTER_MAX and word[6:] == b"\x00\x00":
                note = "<ptr?>"
            words.append(f"{word.hex()}{note}")
        say(f"      {line_start:4d}: {'  '.join(words)}")


def read_struct_bytes(weapon: UObject, wrapped: WrappedStruct, offset_in_weapon: int, size: int) -> bytes | None:
    """The raw bytes of a struct that lives inside the weapon object. Refuses to read when
    the wrapper's address and the offset arithmetic disagree, because then one of them is
    not pointing into the live object."""
    computed = int(weapon._get_address()) + offset_in_weapon
    reported = int(wrapped._get_address())
    if computed != reported:
        say(f"   address mismatch: computed {computed:#x}, wrapper reports {reported:#x}; not reading")
        return None
    return ctypes.string_at(computed, min(size, MAX_DUMP_BYTES))


def dump_candidate_fields(weapon: UObject) -> None:
    say("   fields with identity-looking names on the weapon:")
    for prop in weapon.Class._properties():
        name = str(prop.Name)
        prop_class = str(prop.Class.Name)
        if "Delegate" in prop_class or not CANDIDATE_FIELD_PATTERN.search(name):
            continue
        say(f"      {name}: {prop_class} = {described(lambda: getattr(weapon, name))}")


def dump_identity_call(weapon: UObject, size: int) -> None:
    """InventoryBase.GetIdentity returns the struct by value; its bytes show whether the
    getter hands out the same data as the field."""
    try:
        copy = weapon.GetIdentity()
    except Exception as ex:  # noqa: BLE001
        say(f"   GetIdentity(): <err {type(ex).__name__}: {ex}>")
        return
    if not isinstance(copy, WrappedStruct):
        say(f"   GetIdentity(): {described(lambda: copy)}")
        return
    data = ctypes.string_at(int(copy._get_address()), min(size, MAX_DUMP_BYTES))
    say(f"   GetIdentity() copy: {data.hex()}")


def dump_weapon(slot: int, weapon: UObject) -> None:
    item_prop = struct_property(weapon.Class, "Item")
    data_prop = struct_property(item_prop.Struct, "data")
    identity_prop = struct_property(data_prop.Struct, "Identity")
    item_offset = int(item_prop.Offset_Internal)
    data_offset = item_offset + int(data_prop.Offset_Internal)
    identity_offset = data_offset + int(identity_prop.Offset_Internal)
    item_size = int(item_prop.Struct._get_struct_size())
    identity_size = int(identity_prop.Struct._get_struct_size())
    instance_id = int(weapon.Item.data.InstanceId)
    stem = body_stem(weapon)
    say(f"slot {slot}: {stem} InstanceId={instance_id} {weapon._path_name()}")
    say(
        f"   offsets in the weapon: Item +{item_offset} ({item_size} bytes), data +{data_offset}, "
        f"Identity +{identity_offset} ({identity_size} bytes)"
    )
    item_bytes = read_struct_bytes(weapon, weapon.Item, item_offset, item_size)
    if item_bytes is not None:
        marks = {data_offset - item_offset: "data", identity_offset - item_offset: "Identity"}
        dump_bytes("Item (GbxItem) raw", item_bytes, marks)
    identity_bytes = read_struct_bytes(weapon, weapon.Item.data.Identity, identity_offset, identity_size)
    if identity_bytes is not None:
        # One machine-readable line per weapon for compare_identity.py.
        say(f"identity run={RUN_STAMP} slot={slot} body={stem} id={instance_id} bytes={identity_bytes.hex()}")
    dump_identity_call(weapon, identity_size)
    dump_candidate_fields(weapon)


def main() -> None:
    say(f"=== probe_identity run {RUN_STAMP} ===")
    pc = get_pc()
    pawn = pc.Pawn if pc is not None else None
    if pawn is None:
        say("no pawn; load into the world first")
        return
    weapons = equipped_weapons(pawn)
    say(f"{len(weapons)} equipped weapons")
    for slot, weapon in enumerate(weapons):
        try:
            dump_weapon(slot, weapon)
        except Exception as ex:  # noqa: BLE001
            say(f"slot {slot}: failed: {type(ex).__name__}: {ex}")
    say("=== probe_identity done ===")


main()
