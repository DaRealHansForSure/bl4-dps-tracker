"""
Phase 0 probe #22: read the gun's part definitions straight from its item identity.

probe_names6 showed that `_experimental_alloc_and_mem_leak()` only allocates a blank def,
and that the item identity's first array holds twelve pointers into the same region as its
two head pointers (0x7ff4cc...): the resolved definitions of the gun's parts, as the game
holds them. This probe reads them with ctypes and the reflected layouts:

1. layouts: the offsets and sizes of the fields of InventoryPartDef, InventoryNamingAspect
   and InventoryNamePartDef, and the element sizes of inline structs and def pointers;
2. the FName indices of the mesh part names (raw, from the mesh component), so a raw
   TArray<FName> can be matched against them;
3. for every part pointer of the identity: its GestaltPartNames matched to the mesh names
   (proof that these are InventoryPartDef instances), then its Aspects; for each aspect the
   type pointer is compared with the InventoryNamingAspect struct, and the name part lists
   are followed to the InventoryNamePartDef instances and their PartName text, read as an
   FText display string;
4. the two head pointers, dumped and matched against InventoryTypeDef and InventoryCompDef
   layouts (their FText fields are read too).

    pyexec probe_names7.py    with the gun in hand

Every raw read is checked against VirtualQuery first. No hooks, no calls into the game.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import re
import time
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import get_pc
from unrealsdk import logging
from unrealsdk.unreal import UObject

OUT = Path(__file__).parent / "probe_output.txt"
IDENTITY_SIZE = 216
ARRAY_SLOTS = ((16, 24), (32, 40), (48, 56), (64, 72), (88, 96))
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
MAX_TEXT_CHARS = 200
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE_PROTECTIONS = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)
STRUCTS = {
    "part": "/Script/GbxGame.InventoryPartDef",
    "naming": "/Script/GbxGame.InventoryNamingAspect",
    "name_part": "/Script/GbxGame.InventoryNamePartDef",
    "type": "/Script/GbxGame.InventoryTypeDef",
    "comp": "/Script/GbxGame.InventoryCompDef",
    "gestalt": "/Script/GbxEngine.GestaltDataContainer",
}


class MemoryBasicInformation(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


kernel32 = ctypes.windll.kernel32
kernel32.VirtualQuery.argtypes = (ctypes.c_void_p, ctypes.POINTER(MemoryBasicInformation), ctypes.c_size_t)
kernel32.VirtualQuery.restype = ctypes.c_size_t


def say(msg: str) -> None:
    logging.info(f"[dmgnames7] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def readable(address: int, size: int) -> bool:
    if address < POINTER_MIN or address > POINTER_MAX or size <= 0:
        return False
    cursor = address
    end = address + size
    while cursor < end:
        info = MemoryBasicInformation()
        if kernel32.VirtualQuery(ctypes.c_void_p(cursor), ctypes.byref(info), ctypes.sizeof(info)) == 0:
            return False
        if info.State != MEM_COMMIT or info.Protect & PAGE_GUARD or info.Protect & PAGE_NOACCESS:
            return False
        if not any(info.Protect & flag for flag in READABLE_PROTECTIONS):
            return False
        cursor = (info.BaseAddress or 0) + info.RegionSize
    return True


def read(address: int, size: int) -> bytes | None:
    if not readable(address, size):
        return None
    return ctypes.string_at(address, size)


def u64(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 8], "little")


def i32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "little", signed=True)


def read_tarray(address: int, element_size: int, max_elements: int = 64) -> list[bytes]:
    """The elements of a TArray {data*, num, max} at address, or [] when unreadable."""
    header = read(address, 16)
    if header is None:
        return []
    data, num, capacity = u64(header, 0), i32(header, 8), i32(header, 12)
    if num <= 0 or num > max_elements or capacity < num:
        return []
    raw = read(data, num * element_size)
    if raw is None:
        return []
    return [raw[i * element_size:(i + 1) * element_size] for i in range(num)]


def read_fstring(address: int) -> str | None:
    """An FString {TCHAR* data, num, max} at address."""
    header = read(address, 16)
    if header is None:
        return None
    data, num, capacity = u64(header, 0), i32(header, 8), i32(header, 12)
    if num <= 0 or num > MAX_TEXT_CHARS or capacity < num:
        return None
    raw = read(data, num * 2)
    if raw is None:
        return None
    try:
        return raw.decode("utf-16-le").rstrip("\x00")
    except UnicodeDecodeError:
        return None


def read_ftext(address: int) -> str:
    """The display string of an FText at address: the text data object is scanned for an
    FString (data pointer, num, max) that points at UTF-16 text."""
    header = read(address, 16)
    if header is None:
        return "<text unreadable>"
    text_data = u64(header, 0)
    body = read(text_data, 96)
    if body is None:
        return f"<text data {text_data:#x} unreadable>"
    for offset in range(0, 96 - 16 + 1, 8):
        candidate = read_fstring(text_data + offset)
        if candidate:
            return candidate
    return f"<no string in text data at {text_data:#x}>"


def find_struct(path: str) -> Any:
    """The script struct at a path, or the first one with that name in any package."""
    try:
        return unrealsdk.find_object("ScriptStruct", path)
    except ValueError:
        wanted = path.rsplit(".", 1)[-1]
        for struct in unrealsdk.find_all("ScriptStruct", exact=True):
            if str(struct.Name) == wanted:
                return struct
        raise


def layout(struct_key: str) -> dict[str, tuple[int, int, str, Any]]:
    """name -> (offset, element size, kind, property) for a script struct."""
    struct = find_struct(STRUCTS[struct_key])
    fields = {}
    for prop in struct._properties():
        fields[str(prop.Name)] = (int(prop.Offset_Internal), int(prop.ElementSize), str(prop.Class.Name), prop)
    return fields


def dump_layouts() -> dict[str, dict[str, tuple[int, int, str, Any]]]:
    say("step 1/4: layouts")
    layouts = {}
    for key in ("part", "naming", "name_part", "type", "comp"):
        try:
            fields = layout(key)
        except ValueError as ex:
            say(f"   {key}: {ex}")
            continue
        layouts[key] = fields
        struct = find_struct(STRUCTS[key])
        say(f"   {STRUCTS[key]} ({struct._get_struct_size()} bytes): " + ", ".join(f"{name}@{offset}/{size}:{kind.replace('Property', '')}" for name, (offset, size, kind, _prop) in fields.items()))
        for name, (_offset, _size, kind, prop) in fields.items():
            if kind == "ArrayProperty":
                try:
                    inner = prop.Inner
                    say(f"      {name} elements: {inner.Class.Name} of {inner.ElementSize} bytes")
                except Exception as ex:  # noqa: BLE001
                    say(f"      {name} elements: <err {type(ex).__name__}>")
    return layouts


def mesh_name_indices(weapon: UObject) -> dict[int, str]:
    """FName index -> text for the mesh part names, read raw from the first mesh component."""
    say("step 2/4: mesh part names and their FName indices")
    indices: dict[int, str] = {}
    for component in weapon.K2_GetComponentsByClass(unrealsdk.find_class("SkeletalMeshComponent")):
        try:
            gestalt = component.GestaltData
            names = [str(name) for name in list(gestalt.PartNames)]
        except Exception:  # noqa: BLE001
            continue
        try:
            fields = layout("gestalt")
            offset = fields["PartNames"][0]
            element_size = fields["PartNames"][3].Inner.ElementSize
        except Exception as ex:  # noqa: BLE001
            say(f"   gestalt layout: <err {type(ex).__name__}: {ex}>")
            return indices
        elements = read_tarray(int(gestalt._get_address()) + offset, element_size)
        for text, raw in zip(names, elements):
            indices[i32(raw, 0)] = text
        say(f"   {len(names)} names, FName element size {element_size}: " + ", ".join(f"{text}={i32(raw, 0)}#{i32(raw, 4)}" for text, raw in zip(names, elements)))
        break
    return indices


def fname_text(raw: bytes, known: dict[int, str]) -> str:
    index, number = i32(raw, 0), i32(raw, 4)
    text = known.get(index)
    return f"{text}" if text else f"#{index}.{number}"


def dump_name_part(pointer: int, layouts: dict, indent: str) -> None:
    fields = layouts["name_part"]
    text = read_ftext(pointer + fields["PartName"][0])
    priority_raw = read(pointer + fields["priority"][0], 4)
    priority = ctypes.c_float.from_buffer_copy(priority_raw).value if priority_raw else float("nan")
    stages = read(pointer + fields["mingamestage"][0], 8)
    say(f"{indent}name part {pointer:#x}: {text!r} priority {priority:.2f} stages {i32(stages, 0) if stages else '?'}-{i32(stages, 4) if stages else '?'}")


def dump_aspects(pointer: int, layouts: dict, indent: str) -> None:
    part_fields = layouts["part"]
    aspects_offset, _size, _kind, aspects_prop = part_fields["Aspects"]
    inline_size = int(aspects_prop.Inner.ElementSize)
    naming_struct = find_struct(STRUCTS["naming"])
    naming_address = int(naming_struct._get_address())
    elements = read_tarray(pointer + aspects_offset, inline_size, 32)
    say(f"{indent}{len(elements)} aspect(s), inline element size {inline_size}")
    naming_fields = layouts["naming"]
    def_ptr_size = int(naming_fields["TitlePartList"][3].Inner.ElementSize)
    for index, raw in enumerate(elements):
        words = [u64(raw, offset) for offset in range(0, len(raw) - 7, 8)]
        type_hint = "naming aspect" if naming_address in words else ""
        say(f"{indent}   aspect {index}: {raw.hex()} {type_hint}")
        if not type_hint:
            continue
        # The inline struct: the type pointer at one word, the data either inline after it or
        # behind another pointer; try both readings of the three def pointer lists.
        type_index = words.index(naming_address)
        candidates = [(pointer + aspects_offset, "unused")]
        candidates = []
        for word_index, word in enumerate(words):
            if word_index != type_index and POINTER_MIN <= word <= POINTER_MAX and readable(word, 64):
                candidates.append((word, f"behind word {word_index}"))
        inline_start = (type_index + 1) * 8
        if inline_start + 32 <= len(raw):
            candidates.append((0, f"inline from byte {inline_start}"))
        for base, how in candidates:
            for list_name in ("PrefixPartList", "TitlePartList", "SuffixPartList"):
                list_offset = naming_fields[list_name][0]
                if base:
                    entries = read_tarray(base + list_offset, def_ptr_size, 16)
                else:
                    entries = []
                    header = raw[inline_start + list_offset:inline_start + list_offset + 16]
                    if len(header) == 16:
                        data, num, capacity = u64(header, 0), i32(header, 8), i32(header, 12)
                        if 0 < num <= 16 and capacity >= num:
                            block = read(data, num * def_ptr_size)
                            if block:
                                entries = [block[i * def_ptr_size:(i + 1) * def_ptr_size] for i in range(num)]
                if not entries:
                    continue
                say(f"{indent}      {list_name} ({how}): {len(entries)} entries of {def_ptr_size} bytes")
                for entry in entries:
                    entry_words = [u64(entry, offset) for offset in range(0, len(entry) - 7, 8)]
                    targets = [word for word in entry_words if word >> 40 == 0x7f and readable(word, 64)]
                    say(f"{indent}         entry {entry.hex()} -> {[f'{t:#x}' for t in targets]}")
                    for target in targets:
                        dump_name_part(target, layouts, indent + "            ")


def dump_parts(weapon: UObject, layouts: dict, known_names: dict[int, str]) -> None:
    say("step 3/4: the identity's part pointers")
    identity = weapon.Item.data.Identity
    raw = read(int(identity._get_address()), IDENTITY_SIZE)
    if raw is None:
        say("   identity unreadable")
        return
    part_fields = layouts["part"]
    gestalt_offset, _s, _k, gestalt_prop = part_fields["GestaltPartNames"]
    fname_size = int(gestalt_prop.Inner.ElementSize)
    for data_offset, count_offset in ARRAY_SLOTS:
        elements = read_tarray(int(identity._get_address()) + data_offset, 8)
        say(f"   array at {data_offset}: {len(elements)} pointer(s)")
        for index, element in enumerate(elements):
            pointer = u64(element, 0)
            if not readable(pointer, 176):
                say(f"      [{index}] {pointer:#x} unreadable")
                continue
            names = [fname_text(entry, known_names) for entry in read_tarray(pointer + gestalt_offset, fname_size)]
            removed = [fname_text(entry, known_names) for entry in read_tarray(pointer + part_fields["RemoveGestaltPartNames"][0], fname_size)]
            description = read_fstring(pointer + part_fields["DebugDisplayDescription"][0])
            say(f"      [{index}] {pointer:#x} gestalt {names} removes {removed} description {description!r}")
            if data_offset == 16:
                try:
                    dump_aspects(pointer, layouts, "         ")
                except Exception as ex:  # noqa: BLE001
                    say(f"         aspects: <err {type(ex).__name__}: {ex}>")


def dump_heads(weapon: UObject, layouts: dict) -> None:
    say("step 4/4: the identity's head pointers as type and composition definitions")
    identity = weapon.Item.data.Identity
    raw = read(int(identity._get_address()), 16)
    if raw is None:
        return
    for offset in (0, 8):
        pointer = u64(raw, offset)
        say(f"   head {offset}: {pointer:#x}")
        for key in ("type", "comp"):
            fields = layouts.get(key, {})
            for name, (field_offset, _size, kind, _prop) in fields.items():
                if kind == "TextProperty":
                    say(f"      as {key}.{name}: {read_ftext(pointer + field_offset)!r}")
                elif kind == "StrProperty":
                    say(f"      as {key}.{name}: {read_fstring(pointer + field_offset)!r}")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v7")
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
    try:
        layouts = dump_layouts()
        known = mesh_name_indices(weapon)
        dump_parts(weapon, layouts, known)
        dump_heads(weapon, layouts)
    except Exception as ex:  # noqa: BLE001
        say(f"   failed: {type(ex).__name__}: {ex}")
say("done")
