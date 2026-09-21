"""
Phase 0 probe #26: the naming strategy, the second head, and the UI scripts' variables.

Known so far (probes #20 to #25): the identity's first head is the gun's InventoryTypeDef
("Jakobs Shotgun", MaxNumPrefixes 3, MaxNumSuffixes 2), the barrel's naming aspect gives the
title "T.K's Wave", and the finished "Maintained Screeching T.K's Wave" exists on the heap in
a few UI copies. The prefixes are chosen by the naming strategy. This probe, without any
memory scan (no freeze):

1. reads the type def's NamingStrategyDef pointer, finds the struct type behind it by its
   UScriptStruct address, prints that struct's layout and the strategy's fields, following
   every def pointer list to name part texts;
2. prints the raw headers of the type def's own prefix / title / suffix lists;
3. identifies the identity's second head: its 24-byte def pointers are matched against
   every UScriptStruct address, and it is read as rarity and manufacturer definitions;
4. lists the text-like variables of every UI script and widget Blueprint instance
   (/Game/UI/...): a Blueprint variable holding the focused item's name would be a
   reflected road to the finished name.

    pyexec probe_names11.py    with the gun in hand and the inventory screen open on it

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
from unrealsdk.unreal import UObject, WrappedArray, WrappedStruct

OUT = Path(__file__).parent / "probe_output.txt"
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
MAX_TEXT_CHARS = 200
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE_PROTECTIONS = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)
TYPE_STRUCT = "/Script/GbxGame.InventoryTypeDef"
NAME_PART_STRUCT = "/Script/GbxGame.InventoryNamePartDef"
DEF_PTR_SIZE = 24
TEXT_KINDS = ("TextProperty", "StrProperty", "NameProperty")
UI_PREFIX = "/Game/UI/"
MAX_VALUE_CHARS = 240


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
    logging.info(f"[dmgnames11] {msg}")
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


def f32(data: bytes, offset: int) -> float:
    return ctypes.c_float.from_buffer_copy(data[offset:offset + 4]).value


def fstring_at(address: int) -> str | None:
    header = read(address, 16)
    if header is None:
        return None
    data, num, capacity = u64(header, 0), i32(header, 8), i32(header, 12)
    if num <= 1 or num > MAX_TEXT_CHARS or capacity < num or capacity > 4096:
        return None
    raw = read(data, num * 2)
    if raw is None or raw[-2:] != b"\x00\x00":
        return None
    try:
        text = raw[:-2].decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    if not text or not all(32 <= ord(ch) < 0x3000 for ch in text):
        return None
    return text


def display_string(text_data: int, depth: int = 0) -> str | None:
    body = read(text_data, 96)
    if body is None:
        return None
    for offset in range(0, 96 - 16 + 1, 8):
        found = fstring_at(text_data + offset)
        if found:
            return found
    if depth >= 2:
        return None
    for offset in range(0, 96 - 8 + 1, 8):
        word = u64(body, offset)
        if POINTER_MIN <= word <= POINTER_MAX and word != text_data:
            found = fstring_at(word)
            if found:
                return found
            found = display_string(word, depth + 1)
            if found:
                return found
    return None


def read_ftext(address: int) -> str | None:
    header = read(address, 16)
    if header is None:
        return None
    return display_string(u64(header, 0))


def read_tarray(address: int, element_size: int, max_elements: int = 64) -> list[bytes]:
    header = read(address, 16)
    if header is None:
        return []
    data, num, capacity = u64(header, 0), i32(header, 8), i32(header, 12)
    if num <= 0 or num > max_elements or capacity < num or capacity > 4096:
        return []
    raw = read(data, num * element_size)
    if raw is None:
        return []
    return [raw[i * element_size:(i + 1) * element_size] for i in range(num)]


def find_struct(path: str) -> Any:
    try:
        return unrealsdk.find_object("ScriptStruct", path)
    except ValueError:
        wanted = path.rsplit(".", 1)[-1]
        for struct in unrealsdk.find_all("ScriptStruct", exact=True):
            if str(struct.Name) == wanted:
                return struct
        raise


def structs_by_address() -> dict[int, Any]:
    return {int(struct._get_address()): struct for struct in unrealsdk.find_all("ScriptStruct", exact=True)}


def props_of(struct: Any) -> list[Any]:
    return list(struct._properties())


def describe_field(base: int, prop: Any, name_part_offsets: dict[str, int], indent: str) -> None:
    """Print one field of a definition instance at base, following def pointer lists."""
    kind = str(prop.Class.Name)
    offset = int(prop.Offset_Internal)
    address = base + offset
    if kind == "TextProperty":
        say(f"{indent}{prop.Name}: Text = {read_ftext(address)!r}")
    elif kind == "StrProperty":
        say(f"{indent}{prop.Name}: Str = {fstring_at(address)!r}")
    elif kind in ("IntProperty", "UInt32Property"):
        raw = read(address, 4)
        say(f"{indent}{prop.Name}: Int = {i32(raw, 0) if raw else '?'}")
    elif kind == "FloatProperty":
        raw = read(address, 4)
        say(f"{indent}{prop.Name}: Float = {f32(raw, 0) if raw else '?'}")
    elif kind in ("BoolProperty",):
        raw = read(address, 1)
        say(f"{indent}{prop.Name}: Bool = {bool(raw[0]) if raw else '?'}")
    elif kind == "GbxDefPtrProperty":
        raw = read(address, DEF_PTR_SIZE)
        if raw:
            say(f"{indent}{prop.Name}: DefPtr words {u64(raw, 0):#x} {u64(raw, 8):#x} {u64(raw, 16):#x}")
    elif kind == "ArrayProperty":
        inner = prop.Inner
        inner_kind = str(inner.Class.Name)
        element_size = int(inner.ElementSize)
        entries = read_tarray(address, element_size, 64)
        say(f"{indent}{prop.Name}: Array<{inner_kind}> x{len(entries)}")
        if inner_kind == "GbxDefPtrProperty":
            for entry in entries:
                target = u64(entry, 16)
                text = read_ftext(target + name_part_offsets["PartName"]) if readable(target, 192) else None
                priority_raw = read(target + name_part_offsets["priority"], 4) if readable(target, 192) else None
                say(f"{indent}   -> {target:#x} name part {text!r} priority {f32(priority_raw, 0) if priority_raw else '?'} (type word {u64(entry, 8):#x})")
        elif inner_kind == "NameProperty":
            say(f"{indent}   raw fnames {[f'{i32(e, 0)}.{i32(e, 4)}' for e in entries]}")
    else:
        say(f"{indent}{prop.Name}: {kind} @{offset}")


def dump_strategy(head: int, type_offsets: dict[str, int], name_part_offsets: dict[str, int], by_address: dict[int, Any]) -> None:
    say("step 1: the naming strategy")
    raw = read(head + type_offsets["NamingStrategyDef"], DEF_PTR_SIZE)
    if raw is None:
        say("   unreadable")
        return
    name_word, type_word, resolved = u64(raw, 0), u64(raw, 8), u64(raw, 16)
    struct = by_address.get(type_word)
    say(f"   NamingStrategyDef words {name_word:#x} {type_word:#x} {resolved:#x}; type struct {struct._path_name() if struct is not None else 'unknown'}")
    if struct is None or not readable(resolved, 64):
        # Maybe the type word is not a struct pointer; try the resolved def for text anyway.
        say(f"   raw at resolved: {read(resolved, 64).hex() if read(resolved, 64) else 'unreadable'}")
        return
    say(f"   {struct._path_name()} ({struct._get_struct_size()} bytes): " + ", ".join(f"{p.Name}@{int(p.Offset_Internal)}:{str(p.Class.Name).replace('Property', '')}" for p in props_of(struct)))
    for prop in props_of(struct):
        describe_field(resolved, prop, name_part_offsets, "      ")


def dump_type_lists(head: int, type_offsets: dict[str, int], name_part_offsets: dict[str, int]) -> None:
    say("step 2: the type def's own lists and def pointers")
    struct = find_struct(TYPE_STRUCT)
    for prop in props_of(struct):
        if str(prop.Class.Name) in ("ArrayProperty", "GbxDefPtrProperty", "TextProperty", "IntProperty", "BoolProperty", "StrProperty"):
            describe_field(head, prop, name_part_offsets, "   ")


def dump_second_head(head: int, by_address: dict[int, Any], name_part_offsets: dict[str, int]) -> None:
    say("step 3: the second head")
    raw = read(head, 256)
    if raw is None:
        say("   unreadable")
        return
    say("   raw: " + " ".join(raw[i:i + 8].hex() for i in range(0, 128, 8)))
    # Any def pointer inside it names a struct type by its second word.
    for offset in range(0, 256 - 24 + 1, 8):
        struct = by_address.get(u64(raw, offset + 8))
        if struct is not None:
            say(f"   def pointer at +{offset}: type {struct.Name}, resolved {u64(raw, offset + 16):#x}")
    for struct_name in ("InventoryRarityDef", "InventoryCompDef", "InventoryManufacturerDef", "ManufacturerDef"):
        try:
            struct = find_struct(f"/Script/GbxGame.{struct_name}")
        except ValueError:
            say(f"   {struct_name}: no such struct")
            continue
        say(f"   as {struct_name}:")
        for prop in props_of(struct):
            if str(prop.Class.Name) in ("TextProperty", "StrProperty", "NameProperty"):
                describe_field(head, prop, name_part_offsets, "      ")


def described(value: Any) -> str:
    try:
        if isinstance(value, UObject):
            return f"{value.Class.Name}'{value._path_name()}'"
        if isinstance(value, (WrappedStruct, WrappedArray)):
            return repr(value)[:MAX_VALUE_CHARS]
        text = repr(value)
        return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "..."
    except Exception as ex:  # noqa: BLE001
        return f"<err {type(ex).__name__}>"


def dump_ui_script_texts() -> None:
    say("step 4: text-like variables of the UI Blueprint instances")
    shown = 0
    for cls in unrealsdk.find_all("BlueprintGeneratedClass", exact=True):
        path = str(cls._path_name())
        if not path.startswith(UI_PREFIX):
            continue
        text_props = [prop for prop in cls._properties() if str(prop.Class.Name) in TEXT_KINDS]
        if not text_props:
            continue
        for obj in unrealsdk.find_all(str(cls.Name), exact=True):
            values = []
            for prop in text_props:
                try:
                    value = str(getattr(obj, str(prop.Name)))
                except Exception:  # noqa: BLE001
                    continue
                if re.search(r"[A-Za-z]{3}", value) and value != "None":
                    values.append(f"{prop.Name}={value!r}")
            if values:
                shown += 1
                say(f"   {obj._path_name()}: {' | '.join(values)[:1200]}")
    say(f"   {shown} instance(s) with text")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v11")
pc = get_pc()
pawn = pc.Pawn if pc is not None else None
weapon = None
try:
    weapon = pawn.ActiveWeapons.Slots[0].Weapon if pawn is not None else None
except Exception:  # noqa: BLE001
    weapon = None
try:
    by_address = structs_by_address()
    type_offsets = {str(p.Name): int(p.Offset_Internal) for p in props_of(find_struct(TYPE_STRUCT))}
    name_part_offsets = {str(p.Name): int(p.Offset_Internal) for p in props_of(find_struct(NAME_PART_STRUCT))}
    if weapon is None:
        say("no weapon in hand; skipping the identity steps")
    else:
        identity = int(weapon.Item.data.Identity._get_address())
        heads = read(identity, 16)
        head0, head1 = u64(heads, 0), u64(heads, 8)
        say(f"   heads {head0:#x} {head1:#x}")
        dump_strategy(head0, type_offsets, name_part_offsets, by_address)
        dump_type_lists(head0, type_offsets, name_part_offsets)
        dump_second_head(head1, by_address, name_part_offsets)
    dump_ui_script_texts()
except Exception as ex:  # noqa: BLE001
    say(f"   failed: {type(ex).__name__}: {ex}")
say("done")
