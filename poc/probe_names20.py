"""
Phase 0 probe #35: the identity's own maps, the prefix rows typed, the globals by name.

probe_names19 decoded the licensed-part table: a TMap of row name -> row struct, the row
struct having one Text per licensed magazine kind (default / tor_mag / cov_mag / borg_mag)
and rows for the other licensed part kinds (default, jak_barrel_acc, hyp_shield,
licensed_ted). The identity (216 bytes) also holds a TMap with 8 FName keys at +64, which
may say which parts are licensed. This probe:

1. prints the identity's map at +64 (keys as names, values decoded) and its array at +48;
2. reads every row of both prefix tables through the typed view of the user-defined row
   struct (all columns);
3. finds the inventory globals by the FNames it must contain (BaseWeaponTypeName followed
   by BaseShieldTypeName): one arena scan for the "Weapon" name, neighbours checked;
   then reads InventoryNamingAttributes and WeaponPartAttributes through the typed view;
4. prints the type def's ClassData (inline struct) and the base type chain.

    pyexec probe_names20.py    with the shotgun in hand

Raw reads are checked against VirtualQuery. Only scratch memory the SDK owns is written.
No hooks, no calls into the game.
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
STRATEGY_STRUCT = "/Script/GbxGame.InventoryNamingStrategyDef"
GLOBALS_STRUCT = "/Script/OakGame.OakGlobalsInventory"
SCRATCH_STRUCT = "InventoryTypeDef"
SCRATCH_FIELD = "BaseType"
SCRATCH_NAME_STRUCT = "InventoryPartCriterionData"
MAX_VALUE_CHARS = 1500
MAX_ITEMS = 48
MAP_ELEMENT_SIZE = 24
ROW_TABLE_OFFSET = 80
GLOBALS_WEAPON_NAME_OFFSET = 120
GLOBALS_MAP_OFFSET = 624
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
    logging.info(f"[dmgnames20] {msg}")
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
        self.cache: dict[bytes, str] = {}

    def resolve_raw(self, raw: bytes) -> str:
        cached = self.cache.get(raw)
        if cached is not None:
            return cached
        index = i32(raw, 0)
        if index < 0 or index > 50_000_000:
            text = f"<name {index}>"
        else:
            ctypes.memmove(self.address, raw, 8)
            try:
                text = str(self.scratch.Name)
            except Exception as ex:  # noqa: BLE001
                text = f"<name {index}: {type(ex).__name__}>"
        self.cache[raw] = text
        return text

    def raw_of(self, text: str) -> bytes:
        """The 8 raw bytes of an FName, by letting the SDK create it."""
        self.scratch.Name = text
        return ctypes.string_at(self.address, 8)


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


def decode_pointer(pointer: int, names: NameResolver, by_address: dict[int, Any]) -> str:
    raw = read(pointer, 48)
    if raw is None:
        return "unreadable"
    notes = [f"raw {hex_words(raw)}"]
    struct = by_address.get(u64(raw, 8))
    if struct is not None:
        notes.append(f"def pointer of type {struct.Name} -> {u64(raw, 16):#x}")
    text = fstring_at(pointer)
    if text:
        notes.append(f"FString {text!r}")
    notes.append(f"name {names.resolve_raw(raw[:8])!r}")
    return "; ".join(notes)


def dump_identity_maps(identity: int, names: NameResolver, by_address: dict[int, Any]) -> None:
    say("step 1: the identity's map at +64 and array at +48")
    raw = read(identity, 216)
    say(f"   identity raw: {hex_words(raw)}")
    data, num = u64(raw, 64), i32(raw, 72)
    say(f"   map at +64: data {data:#x} num {num}")
    block = read(data, num * MAP_ELEMENT_SIZE) if is_pointer(data) and 0 < num <= 64 else None
    if block is not None:
        for index in range(num):
            element = block[index * MAP_ELEMENT_SIZE:(index + 1) * MAP_ELEMENT_SIZE]
            key = names.resolve_raw(element[:8])
            value = u64(element, 8)
            say(f"      {key!r} -> {value:#x} (next {i32(element, 16)}, hash {i32(element, 20)}): {decode_pointer(value, names, by_address) if is_pointer(value) else value}")
    data, num = u64(raw, 48), i32(raw, 56)
    say(f"   array at +48: data {data:#x} num {num}")
    block = read(data, num * 16) if is_pointer(data) and 0 < num <= 64 else None
    if block is not None:
        for index in range(num):
            element = block[index * 16:(index + 1) * 16]
            pointer = u64(element, 0)
            say(f"      [{index}] {hex_words(element)}: {decode_pointer(pointer, names, by_address) if is_pointer(pointer) else ''}")


def dump_table_rows(views: DefView, label: str, table: Any, names: NameResolver) -> None:
    address = int(table._get_address())
    say(f"   {label} {def_label(table)}")
    row_struct = table.RowStructPath
    if row_struct is None:
        say("      row struct not loaded")
        return
    fields = [str(p.Name) for p in row_struct._properties()]
    say(f"      columns: {fields}")
    raw = read(address, 160)
    data, num = u64(raw, ROW_TABLE_OFFSET), i32(raw, ROW_TABLE_OFFSET + 8)
    block = read(data, num * MAP_ELEMENT_SIZE) if is_pointer(data) and 0 < num <= 64 else None
    if block is None:
        say("      rows unreadable")
        return
    for index in range(num):
        element = block[index * MAP_ELEMENT_SIZE:(index + 1) * MAP_ELEMENT_SIZE]
        row_name = names.resolve_raw(element[:8])
        row_address = u64(element, 8)
        if not readable(row_address, int(row_struct._get_struct_size())):
            say(f"      {row_name!r}: unreadable row at {row_address:#x}")
            continue
        row = views.view(row_struct, row_address)
        values = []
        for field in fields:
            try:
                values.append(f"{field.split('_')[0]}={described(getattr(row, field))}")
            except Exception as ex:  # noqa: BLE001
                values.append(f"{field}=<err {type(ex).__name__}>")
        say(f"      {row_name!r}: {', '.join(values)}")


def find_globals(arena: Arena, names: NameResolver, views: DefView) -> None:
    say("step 3: the globals by the names they hold")
    weapon_name = names.raw_of("Weapon")
    hits = arena.find_all(weapon_name, 8, 4096)
    say(f"   {len(hits)} word(s) equal the FName 'Weapon'")
    globals_struct = find_struct(GLOBALS_STRUCT)
    found = 0
    for hit in hits:
        neighbours = read(hit, 8 * 8)
        if neighbours is None:
            continue
        following = [names.resolve_raw(neighbours[offset:offset + 8]) for offset in range(8, 64, 8)]
        if not all(name and not name.startswith("<") and name != "None" for name in following[:3]):
            continue
        say(f"   candidate at {hit:#x}: following names {following}")
        instance = hit - GLOBALS_WEAPON_NAME_OFFSET
        if not readable(instance, 2064):
            continue
        found += 1
        view = views.view(globals_struct, instance)
        for field in ("BaseWeaponTypeName", "BaseShieldTypeName", "BaseGadgetTypeName", "GrenadeGadgetTypeName", "RarityDefOrder", "WeaponPartAttributes", "Equipment", "DefaultRepairKit"):
            try:
                say(f"      {field} = {described(getattr(view, field), 0, 3)}")
            except Exception as ex:  # noqa: BLE001
                say(f"      {field} = <err {type(ex).__name__}: {ex}>")
        try:
            say(f"      InventoryNamingAttributes = {described(view.InventoryNamingAttributes, 0, 5)}")
        except Exception as ex:  # noqa: BLE001
            say(f"      InventoryNamingAttributes = <err {type(ex).__name__}: {ex}>")
        raw = read(instance + GLOBALS_MAP_OFFSET, 80)
        data, num = u64(raw, 0), i32(raw, 8)
        say(f"      map raw {hex_words(raw)}; data {data:#x} num {num}")
        block = read(data, min(num, 16) * 80) if is_pointer(data) and 0 < num <= 64 else None
        if block is not None:
            say(f"      map elements: {hex_words(block)}")
            for element_size in (24, 32, 40, 48, 56, 64):
                keys = [names.resolve_raw(block[i * element_size:i * element_size + 8]) for i in range(min(num, 8)) if (i + 1) * element_size <= len(block)]
                say(f"         as {element_size}-byte elements, keys {keys}")
        try:
            attributes = view.WeaponPartAttributes
            say(f"      WeaponPartAttributes {def_label(attributes)}: {described(attributes, 0, 5)}")
        except Exception as ex:  # noqa: BLE001
            say(f"      WeaponPartAttributes: <err {type(ex).__name__}: {ex}>")
        if found >= 2:
            break
    if found == 0:
        say("   no globals candidate in the arena")


def dump_class_data(views: DefView, type_struct: Any, head0: int) -> None:
    say("step 4: ClassData and the base type chain")
    address = head0
    for depth in range(6):
        view = views.view(type_struct, address)
        try:
            base = view.BaseType
            say(f"   type at {address:#x}: {view.ItemBaseName!s} / slot {view.slot!s}; ClassData = {described(view.ClassData, 0, 4)}")
            say(f"      lists: prefixes {len(list(view.PrefixPartList))}, titles {len(list(view.TitlePartList))}, suffixes {len(list(view.SuffixPartList))}; MaxNumPrefixes {view.MaxNumPrefixes}; NamingStrategyDef {def_label(view.NamingStrategyDef)}")
            next_address = int(base._get_address())
        except Exception as ex:  # noqa: BLE001
            say(f"   type at {address:#x}: <err {type(ex).__name__}: {ex}>")
            break
        if not next_address or next_address == address:
            break
        address = next_address


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v20: identity maps, prefix rows, globals by name")
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
    views = DefView()
    names = NameResolver()
    if weapon is None:
        say("no weapon in hand")
    else:
        identity = int(weapon.Item.data.Identity._get_address())
        head0 = u64(read(identity, 8), 0)
        type_struct = find_struct(TYPE_STRUCT)
        say(f"   gun: {views.view(type_struct, head0).ItemBaseName!s}")
        dump_identity_maps(identity, names, by_address)
        say("step 2: the prefix table rows, typed")
        strategy = views.view(find_struct(STRATEGY_STRUCT), int(views.view(type_struct, head0).NamingStrategyDef._get_address())).NamingStrategy
        for field in ("MagBarrelDataTable", "PayloadDeliveryDataTable"):
            try:
                dump_table_rows(views, field, getattr(strategy, field), names)
            except Exception as ex:  # noqa: BLE001
                say(f"   {field}: <err {type(ex).__name__}: {ex}>")
        arena = Arena(head0)
        say(f"   arena at {arena.base:#x}: {arena.size / 1e6:.1f} MB")
        find_globals(arena, names, views)
        dump_class_data(views, type_struct, head0)
except Exception as ex:  # noqa: BLE001
    import traceback
    say(f"   failed: {type(ex).__name__}: {ex}")
    say(traceback.format_exc()[-1500:])
finally:
    if views is not None:
        views.restore()
say("done")
