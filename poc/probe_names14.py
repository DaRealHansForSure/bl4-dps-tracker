"""
Phase 0 probe #29: typed views on definitions through a forged def pointer, and the globals.

A `GbxDefPtr` property is a 24-byte triple {name, type struct*, data*}; the SDK wraps its
value as `FGbxDefPtr` with attribute access to the data as the struct type. Writing our own
triple into a scratch struct the SDK owns gives a typed view on any definition instance we
know the address of. This probe:

1. proves the trick on the gun's type definition (ItemBaseName must read "Jakobs Shotgun"),
   then on its manufacturer, rarity and composition definitions and the "T.K's Wave" part;
2. finds the OakGlobalsInventory instance: its RarityDefOrder array holds a def pointer to
   the gun's rarity definition, so the arena is searched for that pointer, then for the
   header pointing at the array;
3. dumps the globals through the typed view, and InventoryNamingAttributes raw as well
   (map elements guessed by their trailing hash links).

    pyexec probe_names14.py    with the gun in hand

Every raw read is checked against VirtualQuery first. Only scratch memory the SDK gave us
is written. No hooks, no calls into the game.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import time
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import get_pc
from unrealsdk import logging
from unrealsdk.unreal import FGbxDefPtr, UObject, WrappedArray, WrappedStruct

OUT = Path(__file__).parent / "probe_output.txt"
POINTER_MIN = 0x10000
POINTER_MAX = 0x7FFFFFFFFFFF
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE_PROTECTIONS = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)
MAX_ARENA_BYTES = 512 * 1024 * 1024
DEF_PTR_SIZE = 24
TYPE_STRUCT = "/Script/GbxGame.InventoryTypeDef"
GLOBALS_STRUCT = "/Script/OakGame.OakGlobalsInventory"
NAME_PART_STRUCT = "/Script/GbxGame.InventoryNamePartDef"
SCRATCH_STRUCT = "InventoryTypeDef"
SCRATCH_FIELD = "BaseType"
MAX_VALUE_CHARS = 700
MAX_ITEMS = 40
MAP_OFFSET = 624
RARITY_ORDER_OFFSET = 2008


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
    logging.info(f"[dmgnames14] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def region(address: int) -> MemoryBasicInformation | None:
    if address < POINTER_MIN or address > POINTER_MAX:
        return None
    info = MemoryBasicInformation()
    if kernel32.VirtualQuery(ctypes.c_void_p(address), ctypes.byref(info), ctypes.sizeof(info)) == 0:
        return None
    return info


def region_readable(info: MemoryBasicInformation) -> bool:
    if info.State != MEM_COMMIT or info.Protect & PAGE_GUARD or info.Protect & PAGE_NOACCESS:
        return False
    return any(info.Protect & flag for flag in READABLE_PROTECTIONS)


def readable(address: int, size: int) -> bool:
    if address < POINTER_MIN or address > POINTER_MAX or size <= 0:
        return False
    cursor = address
    end = address + size
    while cursor < end:
        info = region(cursor)
        if info is None or not region_readable(info):
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


def is_pointer(word: int) -> bool:
    return POINTER_MIN <= word <= POINTER_MAX


def find_struct(path: str) -> Any:
    try:
        return unrealsdk.find_object("ScriptStruct", path)
    except ValueError:
        wanted = path.rsplit(".", 1)[-1]
        for struct in unrealsdk.find_all("ScriptStruct", exact=True):
            if str(struct.Name) == wanted:
                return struct
        raise


def described(value: Any, depth: int = 0) -> str:
    """A compact, bounded rendering of any SDK value."""
    try:
        if isinstance(value, UObject):
            return f"{value.Class.Name}'{value._path_name()}'"
        if isinstance(value, FGbxDefPtr):
            # No repr: the SDK's repr walks nested definitions without a depth limit.
            try:
                return f"<def at {int(value._get_address()):#x}>"
            except Exception:  # noqa: BLE001
                return "<def>"
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


class DefView:
    """Typed access to a definition instance through a forged def pointer in scratch memory."""

    def __init__(self) -> None:
        self.scratch = unrealsdk.make_struct(SCRATCH_STRUCT)
        field = next(p for p in find_struct(TYPE_STRUCT)._properties() if str(p.Name) == SCRATCH_FIELD)
        self.field_address = int(self.scratch._get_address()) + int(field.Offset_Internal)
        self.original = ctypes.string_at(self.field_address, DEF_PTR_SIZE)

    def view(self, struct: Any, data_address: int, name_word: int = 0) -> Any:
        triple = name_word.to_bytes(8, "little") + int(struct._get_address()).to_bytes(8, "little") + data_address.to_bytes(8, "little")
        ctypes.memmove(self.field_address, triple, DEF_PTR_SIZE)
        return getattr(self.scratch, SCRATCH_FIELD)

    def restore(self) -> None:
        ctypes.memmove(self.field_address, self.original, DEF_PTR_SIZE)


def dump_view(label: str, pointer: Any, fields: tuple[str, ...] | None = None) -> None:
    say(f"   {label}: {described(pointer)}")
    struct = None
    names = fields
    if names is None and struct is not None:
        names = tuple(str(p.Name) for p in struct._properties())
    if names is None:
        say("      (no property list available)")
        return
    for name in names:
        try:
            say(f"      {name} = {described(getattr(pointer, name))}")
        except Exception as ex:  # noqa: BLE001
            say(f"      {name} = <err {type(ex).__name__}: {ex}>")


class Arena:
    def __init__(self, sample_pointer: int) -> None:
        info = region(sample_pointer)
        if info is None:
            raise RuntimeError("no region")
        base = info.AllocationBase or info.BaseAddress or 0
        chunks: list[tuple[int, bytes]] = []
        cursor = base
        total = 0
        while total < MAX_ARENA_BYTES:
            part = region(cursor)
            if part is None or (part.AllocationBase or 0) != base:
                break
            size = part.RegionSize
            if region_readable(part) and size <= MAX_ARENA_BYTES:
                chunks.append((cursor, ctypes.string_at(cursor, size)))
                total += size
            cursor += size
        self.base = base
        self.chunks = chunks
        self.size = total

    def find_all(self, needle: bytes, aligned: int = 1, limit: int = 64) -> list[int]:
        hits: list[int] = []
        for start, data in self.chunks:
            position = data.find(needle)
            while position >= 0 and len(hits) < limit:
                if (start + position) % aligned == 0:
                    hits.append(start + position)
                position = data.find(needle, position + 1)
            if len(hits) >= limit:
                break
        return hits

    def pointers_to(self, address: int, limit: int = 64) -> list[int]:
        return self.find_all(address.to_bytes(8, "little"), 8, limit)


def find_globals(arena: Arena, rarity_def: int, by_address: dict[int, Any]) -> list[int]:
    """Globals instances whose RarityDefOrder array contains the gun's rarity definition."""
    rarity_struct = find_struct("/Script/GbxGame.InventoryRarityDef")
    candidates: list[int] = []
    for hit in arena.pointers_to(rarity_def, 128):
        # A def pointer to the rarity def: the type word sits 8 bytes before the data word.
        raw = read(hit - 16, 24)
        if raw is None or by_address.get(u64(raw, 8)) is not rarity_struct:
            continue
        entry = hit - 16
        # Walk back over earlier 24-byte entries of the same kind to the array start.
        start = entry
        while True:
            previous = read(start - DEF_PTR_SIZE, DEF_PTR_SIZE)
            if previous is None or by_address.get(u64(previous, 8)) is not rarity_struct:
                break
            start -= DEF_PTR_SIZE
        for header in arena.pointers_to(start, 16):
            header_raw = read(header, 16)
            if header_raw is None:
                continue
            num, capacity = i32(header_raw, 8), i32(header_raw, 12)
            if 0 < num <= 32 and num <= capacity <= 64:
                instance = header - RARITY_ORDER_OFFSET
                say(f"      rarity def pointer at {entry:#x}, array start {start:#x}, header {header:#x} (num {num}) -> globals candidate {instance:#x}")
                if instance not in candidates:
                    candidates.append(instance)
    return candidates


def guess_map_elements(address: int) -> None:
    header = read(address, 80)
    if header is None:
        say("      map unreadable")
        return
    data, num, capacity = u64(header, 0), i32(header, 8), i32(header, 12)
    say(f"      map elements data {data:#x} num {num} max {capacity}; raw header " + " ".join(header[i:i + 8].hex() for i in range(0, 80, 8)))
    if not is_pointer(data) or num <= 0 or num > 512:
        return
    raw = None
    for size in (min(num, 8) * 512, 2048, 512, 256):
        raw = read(data, size)
        if raw is not None:
            break
    if raw is None:
        say("      elements unreadable")
        return
    say("      first bytes: " + " ".join(raw[i:i + 8].hex() for i in range(0, min(256, len(raw)), 8)))
    if num < 2:
        return
    # Each element ends with {HashNextId, HashIndex}, both small (or -1) for every element.
    for element_size in range(16, 513, 8):
        ok = True
        for index in range(min(num, 8)):
            tail = index * element_size + element_size - 8
            if tail + 8 > len(raw):
                ok = False
                break
            next_id, hash_index = i32(raw, tail), i32(raw, tail + 4)
            if not (-1 <= next_id < num and 0 <= hash_index < 4096):
                ok = False
                break
        if ok:
            say(f"      element size {element_size} fits the hash links")
            for index in range(min(num, 6)):
                element = raw[index * element_size:(index + 1) * element_size]
                say(f"         [{index}] " + " ".join(element[i:i + 8].hex() for i in range(0, len(element), 8)))
            break


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v14: typed definition views")
pc = get_pc()
pawn = pc.Pawn if pc is not None else None
weapon = None
try:
    weapon = pawn.ActiveWeapons.Slots[0].Weapon if pawn is not None else None
except Exception:  # noqa: BLE001
    weapon = None
views: DefView | None = None
try:
    by_address = {int(s._get_address()): s for s in unrealsdk.find_all("ScriptStruct", exact=True)}
    type_struct = find_struct(TYPE_STRUCT)
    views = DefView()
    if weapon is None:
        say("no weapon in hand")
    else:
        identity = int(weapon.Item.data.Identity._get_address())
        heads = read(identity, 16)
        head0, head1 = u64(heads, 0), u64(heads, 8)
        say("step 1: which type does the SDK use for a forged def pointer?")
        type_view = views.view(type_struct, head0)
        say(f"   type def as InventoryTypeDef: ItemBaseName = {described(type_view.ItemBaseName)}, MaxNumPrefixes = {described(type_view.MaxNumPrefixes)}")
        second = read(head1, 128)
        comp_def = rarity_def = 0
        if second is not None:
            comp_def, rarity_def = u64(second, 72 + 16), u64(second, 96 + 16)
        rarity_struct = find_struct("/Script/GbxGame.InventoryRarityDef")
        uses_triple_type = False
        if rarity_def:
            rarity_view = views.view(rarity_struct, rarity_def)
            try:
                say(f"   rarity def as InventoryRarityDef: InternalName = {described(rarity_view.InternalName)}")
                uses_triple_type = True
            except AttributeError as ex:
                say(f"   the SDK uses the property's declared type, not the triple's: {ex}")
        if uses_triple_type:
            dump_view("type def", views.view(type_struct, head0), ("ItemBaseName", "InventoryName", "MaxNumPrefixes", "MaxNumSuffixes", "bInheritBasePartLists", "PrefixPartList", "TitlePartList", "SuffixPartList", "NamingStrategyDef", "BaseType", "Manufacturer", "Rarity", "stats", "UIStats", "PartTypes", "slot"))
            dump_view("rarity def", views.view(rarity_struct, rarity_def), ("InternalName", "DisplayName", "SortValue"))
            dump_view("comp def", views.view(find_struct("/Script/GbxGame.InventoryCompDef"), comp_def), ("ItemBaseName", "BaseComposition", "Rarity", "CriterionTagDisplayName"))
        say("step 2: the inventory globals")
        arena = Arena(head0)
        say(f"   arena at {arena.base:#x}: {arena.size / 1e6:.1f} MB")
        candidates = find_globals(arena, rarity_def, by_address) if rarity_def else []
        say(f"   {len(candidates)} candidate(s)")
        globals_struct = find_struct(GLOBALS_STRUCT)
        for instance in candidates[:3]:
            if uses_triple_type:
                dump_view(f"globals at {instance:#x}", views.view(globals_struct, instance), ("BaseWeaponTypeName", "BaseShieldTypeName", "GrenadeGadgetTypeName", "InventoryNamingAttributes", "WeaponPartAttributes", "RarityDefOrder", "Equipment"))
            guess_map_elements(instance + MAP_OFFSET)
except Exception as ex:  # noqa: BLE001
    import traceback
    say(f"   failed: {type(ex).__name__}: {ex}")
    say(traceback.format_exc()[-1500:])
finally:
    if views is not None:
        views.restore()
say("done")
