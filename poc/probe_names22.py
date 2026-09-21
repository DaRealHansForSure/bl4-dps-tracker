"""
Phase 0 probe #37: the inventory globals, typed, at the addresses probe_names21 found.

Reads probe_names21_result.json and, for every candidate, views the OakGlobalsInventory
through a forged def pointer: the base type names, RarityDefOrder, WeaponPartAttributes
(typed, in depth) and InventoryNamingAttributes (through the SDK if it supports maps, and
raw with the keys resolved as names and the values decoded).

    pyexec probe_names22.py    after "scan finished" appeared in the log

Raw reads are checked against VirtualQuery. Only scratch memory the SDK owns is written.
No hooks, no calls into the game.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import time
from pathlib import Path
from typing import Any

import unrealsdk
from unrealsdk import logging
from unrealsdk.unreal import FGbxDefPtr, UObject, WrappedArray, WrappedStruct

OUT = Path(__file__).parent / "probe_output.txt"
RESULT = Path(__file__).parent / "probe_names21_result.json"
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE_PROTECTIONS = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)
DEF_PTR_SIZE = 24
TYPE_STRUCT = "/Script/GbxGame.InventoryTypeDef"
GLOBALS_STRUCT = "/Script/OakGame.OakGlobalsInventory"
SCRATCH_STRUCT = "InventoryTypeDef"
SCRATCH_FIELD = "BaseType"
SCRATCH_NAME_STRUCT = "InventoryPartCriterionData"
GLOBALS_SIZE = 2064
GLOBALS_MAP_OFFSET = 624
GLOBALS_RARITY_ORDER_OFFSET = 2008
MAX_VALUE_CHARS = 2500
MAX_ITEMS = 64
MAX_TEXT_CHARS = 200


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
    logging.info(f"[dmgnames22] {msg}")
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


def is_pointer(word: int) -> bool:
    return POINTER_MIN <= word <= POINTER_MAX


def hex_words(data: bytes) -> str:
    return " ".join(data[i:i + 8].hex() for i in range(0, len(data) - 7, 8))


def find_struct(path: str) -> Any:
    try:
        return unrealsdk.find_object("ScriptStruct", path)
    except ValueError:
        wanted = path.rsplit(".", 1)[-1]
        for struct in unrealsdk.find_all("ScriptStruct", exact=True):
            if str(struct.Name) == wanted:
                return struct
        raise


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


def def_label(value: Any) -> str:
    address = 0
    try:
        address = int(value._get_address())
    except Exception:  # noqa: BLE001
        pass
    name = kind_name = None
    try:
        name = value._name
        kind = value._type
        kind_name = str(kind.Name) if kind is not None else None
    except Exception:  # noqa: BLE001
        pass
    return f"<def {name} {kind_name} {address:#x}>"


def described(value: Any, depth: int = 0, max_depth: int = 4) -> str:
    try:
        if isinstance(value, UObject):
            return f"{value.Class.Name}'{value._path_name()}'"
        if isinstance(value, FGbxDefPtr):
            return def_label(value)
        if isinstance(value, WrappedStruct):
            if depth >= max_depth:
                return f"<struct {value._type.Name}>"
            parts = []
            for prop in value._type._properties():
                name = str(prop.Name)
                try:
                    parts.append(f"{name}={described(getattr(value, name), depth + 1, max_depth)}")
                except Exception as ex:  # noqa: BLE001
                    parts.append(f"{name}=<err {type(ex).__name__}>")
            return "{" + ", ".join(parts) + "}"
        if isinstance(value, WrappedArray):
            items = [described(item, depth + 1, max_depth) for item in list(value)[:MAX_ITEMS]]
            more = "" if len(value) <= MAX_ITEMS else f", ... {len(value)} items"
            return "[" + ", ".join(items) + more + "]"
        text = repr(value)
        return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "..."
    except Exception as ex:  # noqa: BLE001
        return f"<err {type(ex).__name__}: {ex}>"


class DefView:
    def __init__(self) -> None:
        self.scratch = unrealsdk.make_struct(SCRATCH_STRUCT)
        field = next(p for p in find_struct(TYPE_STRUCT)._properties() if str(p.Name) == SCRATCH_FIELD)
        self.field_address = int(self.scratch._get_address()) + int(field.Offset_Internal)
        self.original = ctypes.string_at(self.field_address, DEF_PTR_SIZE)

    def view(self, struct: Any, data_address: int) -> Any:
        triple = bytes(8) + int(struct._get_address()).to_bytes(8, "little") + data_address.to_bytes(8, "little")
        ctypes.memmove(self.field_address, triple, DEF_PTR_SIZE)
        return getattr(self.scratch, SCRATCH_FIELD)

    def restore(self) -> None:
        ctypes.memmove(self.field_address, self.original, DEF_PTR_SIZE)


class NameResolver:
    def __init__(self) -> None:
        self.scratch = unrealsdk.make_struct(SCRATCH_NAME_STRUCT)
        name_prop = next(p for p in self.scratch._type._properties() if str(p.Class.Name) == "NameProperty")
        self.address = int(self.scratch._get_address()) + int(name_prop.Offset_Internal)

    def resolve_raw(self, raw: bytes) -> str:
        index = i32(raw, 0)
        if index < 0 or index > 50_000_000:
            return f"<name {index}>"
        ctypes.memmove(self.address, raw, 8)
        try:
            return str(self.scratch.Name)
        except Exception as ex:  # noqa: BLE001
            return f"<name {index}: {type(ex).__name__}>"


def decode_map(address: int, names: NameResolver, by_address: dict[int, Any]) -> None:
    raw = read(address, 80)
    if raw is None:
        say("      map unreadable")
        return
    data, num, capacity = u64(raw, 0), i32(raw, 8), i32(raw, 12)
    say(f"      map header: data {data:#x} num {num} max {capacity}; raw {hex_words(raw)}")
    if not is_pointer(data) or num <= 0 or num > 64:
        return
    block = read(data, min(num, 16) * 96)
    if block is None:
        say("      map elements unreadable")
        return
    say(f"      elements raw: {hex_words(block[:num * 48])}")
    for element_size in (24, 32, 40, 48, 56, 64, 72, 80, 96):
        if num * element_size > len(block):
            break
        keys = [names.resolve_raw(block[i * element_size:i * element_size + 8]) for i in range(num)]
        links = [(i32(block, (i + 1) * element_size - 8), i32(block, (i + 1) * element_size - 4)) for i in range(num)]
        plausible = all(-1 <= link[0] < num and 0 <= link[1] < 64 for link in links)
        say(f"      as {element_size}-byte elements{' (links fit)' if plausible else ''}: keys {keys}")
        if plausible:
            for i in range(num):
                element = block[i * element_size:(i + 1) * element_size]
                notes = []
                for offset in range(8, element_size - 8, 8):
                    word = u64(element, offset)
                    struct = by_address.get(word)
                    if struct is not None:
                        notes.append(f"+{offset} type {struct.Name}, resolved {u64(element, offset + 8):#x}")
                    elif is_pointer(word) and readable(word, 16):
                        text = fstring_at(word)
                        if text:
                            notes.append(f"+{offset} FString {text!r}")
                notes.append(f"+8 as name {names.resolve_raw(element[8:16])!r}")
                say(f"         [{i}] {keys[i]!r}: {hex_words(element)} | {' | '.join(notes)}")
            break


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v22: the globals, typed")
views: DefView | None = None
try:
    result = json.loads(RESULT.read_text(encoding="utf-8"))
    candidates = [int(text, 16) for text in result.get("globals", [])]
    candidates += [int(text, 16) - GLOBALS_RARITY_ORDER_OFFSET for text in result.get("headers", [])]
    say(f"   candidates: {[f'{c:#x}' for c in candidates]}; rarity runs {result.get('rarity_runs')}")
    by_address = {int(s._get_address()): s for s in unrealsdk.find_all("ScriptStruct", exact=True)}
    views = DefView()
    names = NameResolver()
    globals_struct = find_struct(GLOBALS_STRUCT)
    for instance in candidates[:4]:
        if not readable(instance, GLOBALS_SIZE):
            say(f"   {instance:#x}: unreadable")
            continue
        say(f"   globals at {instance:#x}")
        view = views.view(globals_struct, instance)
        for field in ("BaseWeaponTypeName", "BaseShieldTypeName", "BaseGadgetTypeName", "BaseRepairKitTypeName", "GrenadeGadgetTypeName", "RarityDefOrder", "Equipment", "DefaultRepairKit"):
            try:
                say(f"      {field} = {described(getattr(view, field), 0, 3)}")
            except Exception as ex:  # noqa: BLE001
                say(f"      {field} = <err {type(ex).__name__}: {ex}>")
        try:
            say(f"      InventoryNamingAttributes (SDK) = {described(view.InventoryNamingAttributes, 0, 5)}")
        except Exception as ex:  # noqa: BLE001
            say(f"      InventoryNamingAttributes (SDK) = <err {type(ex).__name__}: {ex}>")
        decode_map(instance + GLOBALS_MAP_OFFSET, names, by_address)
        try:
            attributes = view.WeaponPartAttributes
            say(f"      WeaponPartAttributes {def_label(attributes)}")
            struct = attributes._type
            if struct is not None:
                say(f"      {struct._path_name()}: " + ", ".join(f"{p.Name}@{int(p.Offset_Internal)}:{str(p.Class.Name).replace('Property', '')}" for p in struct._properties()))
                for prop in struct._properties():
                    say(f"         {prop.Name} = {described(getattr(attributes, str(prop.Name)), 0, 6)}")
        except Exception as ex:  # noqa: BLE001
            say(f"      WeaponPartAttributes: <err {type(ex).__name__}: {ex}>")
except Exception as ex:  # noqa: BLE001
    import traceback
    say(f"   failed: {type(ex).__name__}: {ex}")
    say(traceback.format_exc()[-1500:])
finally:
    if views is not None:
        views.restore()
say("done")
