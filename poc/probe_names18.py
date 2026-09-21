"""
Phase 0 probe #33: stat values, the stats definition, the prefix tables and the globals.

probe_names17 showed the stat modifier parts ("Damage mod" = StatModifierAspect with
stattagname 'damage' and a GbxAttributeInit value) and that the licensed mag of the
shotgun is a Borg part. Still missing for composing a name: the modifier values, how the
naming attributes (damage, firerate, ...) are computed from them, the rows of the
licensed-part prefix tables (Nexus data tables, not UObjects) and the inventory globals
with their InventoryNamingAttributes map. This probe:

1. prints every StatModifierAspect value in full depth (GbxAttributeInit);
2. dumps the type's `stats` definition (InventoryStatsDef) and its UI stats;
3. reads the two prefix tables: RowStructPath through the typed view, then the native part
   of GbxUEDataTableDef raw, rows viewed as the row struct;
4. finds the globals by the rarity order array: consecutive InventoryRarityDef pointers in
   the arena, then the header pointing at that array (three scans at most);
5. lists the functions of the attribute and inventory function libraries.

    pyexec probe_names18.py    with the shotgun in hand

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
PART_STRUCT = "/Script/GbxGame.InventoryPartDef"
STRATEGY_STRUCT = "/Script/GbxGame.InventoryNamingStrategyDef"
TABLE_STRUCT = "/Script/GbxNexus.GbxUEDataTableDef"
GLOBALS_STRUCT = "/Script/OakGame.OakGlobalsInventory"
RARITY_STRUCT = "/Script/GbxGame.InventoryRarityDef"
SCRATCH_STRUCT = "InventoryTypeDef"
SCRATCH_FIELD = "BaseType"
MAX_VALUE_CHARS = 1200
MAX_ITEMS = 48
RARITY_ORDER_OFFSET = 2008
MAP_OFFSET = 624
MIN_RARITY_RUN = 5
LIBRARIES = (
    "GbxAttributeBlueprintLibrary",
    "OakInventoryStatics",
    "ItemsFunctionLibrary",
    "InventoryItemBlueprintLibrary",
    "GbxInventoryStatics",
    "InventoryStatics",
    "OakUIFunctionLibrary",
    "GbxUIFunctionLibrary",
)


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
    logging.info(f"[dmgnames18] {msg}")
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


def dump_stat_modifiers(views: DefView, identity: int) -> None:
    say("step 1: the stat modifier values")
    part_struct = find_struct(PART_STRUCT)
    header = read(identity + 16, 16)
    data, num = u64(header, 0), i32(header, 8)
    raw = read(data, num * 8)
    for index in range(num):
        address = u64(raw, index * 8)
        part = views.view(part_struct, address)
        try:
            gestalt = described(part.GestaltPartNames)
            for aspect in list(part.Aspects):
                type_name = str(aspect._type.Name)
                if type_name == "StatModifierAspect":
                    say(f"   [{index}] {gestalt}: {described(aspect, 0, 8)}")
        except Exception as ex:  # noqa: BLE001
            say(f"   [{index}]: <err {type(ex).__name__}: {ex}>")


def dump_stats_def(views: DefView, type_view: Any) -> None:
    say("step 2: the stats definition")
    stats = type_view.stats
    say(f"   stats {def_label(stats)}")
    try:
        struct = stats._type
        say(f"   {struct._path_name()}: " + ", ".join(f"{p.Name}@{int(p.Offset_Internal)}:{str(p.Class.Name).replace('Property', '')}" for p in struct._properties()))
        for prop in struct._properties():
            say(f"      {prop.Name} = {described(getattr(stats, str(prop.Name)), 0, 5)}")
    except Exception as ex:  # noqa: BLE001
        say(f"   <err {type(ex).__name__}: {ex}>")
    try:
        for index, ui_stat in enumerate(list(type_view.UIStats)):
            say(f"   UIStats[{index}] {def_label(ui_stat)}: {described(ui_stat, 0, 4)}")
    except Exception as ex:  # noqa: BLE001
        say(f"   UIStats: <err {type(ex).__name__}: {ex}>")


def dump_table(views: DefView, label: str, table: Any, by_address: dict[int, Any]) -> None:
    address = int(table._get_address())
    say(f"   {label}: {def_label(table)}")
    try:
        say(f"      RowStructPath = {described(table.RowStructPath)}")
    except Exception as ex:  # noqa: BLE001
        say(f"      RowStructPath: <err {type(ex).__name__}: {ex}>")
    raw = read(address, 160)
    if raw is None:
        say("      unreadable")
        return
    say(f"      raw: {hex_words(raw)}")
    for offset in range(0, 160 - 7, 8):
        word = u64(raw, offset)
        struct = by_address.get(word)
        if struct is not None:
            say(f"      +{offset}: struct {struct._path_name()}")
    # Candidate row storage: a pointer followed by a plausible (num, max) pair.
    for offset in range(64, 160 - 15, 8):
        pointer, num, capacity = u64(raw, offset), i32(raw, offset + 8), i32(raw, offset + 12)
        if is_pointer(pointer) and 0 < num <= 64 and num <= capacity <= 256 and readable(pointer, 64):
            block = read(pointer, min(num * 64, 1024))
            say(f"      +{offset}: pointer {pointer:#x} with num {num} max {capacity}; first bytes {hex_words(block[:256]) if block else '?'}")
    for offset in range(64, 160 - 7, 8):
        pointer = u64(raw, offset)
        if is_pointer(pointer) and readable(pointer, 64) and offset not in (96,):
            block = read(pointer, 128)
            say(f"      +{offset}: pointer {pointer:#x} -> {hex_words(block)}")


def dump_tables(views: DefView, type_view: Any, by_address: dict[int, Any]) -> None:
    say("step 3: the prefix tables")
    strategy = views.view(find_struct(STRATEGY_STRUCT), int(type_view.NamingStrategyDef._get_address())).NamingStrategy
    for field in ("PayloadDeliveryDataTable", "MagBarrelDataTable"):
        try:
            dump_table(views, field, getattr(strategy, field), by_address)
        except Exception as ex:  # noqa: BLE001
            say(f"   {field}: <err {type(ex).__name__}: {ex}>")


def find_globals(arena: Arena, by_address: dict[int, Any], views: DefView) -> None:
    say("step 4: the globals through the rarity order array")
    rarity_struct = find_struct(RARITY_STRUCT)
    hits = set(arena.find_all(int(rarity_struct._get_address()).to_bytes(8, "little"), 8, 20000))
    say(f"   {len(hits)} rarity def pointer(s) in the arena")
    starts: list[int] = []
    for hit in sorted(hits):
        if hit - DEF_PTR_SIZE in hits:
            continue
        run = 1
        while hit + run * DEF_PTR_SIZE in hits:
            run += 1
        if run >= MIN_RARITY_RUN:
            starts.append(hit - 8)
            say(f"   run of {run} at {hit - 8:#x}")
    globals_struct = find_struct(GLOBALS_STRUCT)
    for start in starts[:3]:
        for header in arena.pointers_to(start, 8):
            header_raw = read(header, 16)
            if header_raw is None:
                continue
            num, capacity = i32(header_raw, 8), i32(header_raw, 12)
            if not (MIN_RARITY_RUN <= num <= 32 and num <= capacity <= 64):
                continue
            instance = header - RARITY_ORDER_OFFSET
            say(f"   header {header:#x} (num {num}) -> globals candidate {instance:#x}")
            if not readable(instance, 2064):
                continue
            view = views.view(globals_struct, instance)
            for field in ("BaseWeaponTypeName", "BaseShieldTypeName", "GrenadeGadgetTypeName", "RarityDefOrder", "WeaponPartAttributes", "Equipment"):
                try:
                    say(f"      {field} = {described(getattr(view, field), 0, 3)}")
                except Exception as ex:  # noqa: BLE001
                    say(f"      {field} = <err {type(ex).__name__}: {ex}>")
            try:
                say(f"      InventoryNamingAttributes = {described(view.InventoryNamingAttributes, 0, 4)}")
            except Exception as ex:  # noqa: BLE001
                say(f"      InventoryNamingAttributes = <err {type(ex).__name__}: {ex}>")
            raw = read(instance + MAP_OFFSET, 80)
            if raw is not None:
                say(f"      map raw {hex_words(raw)}")
                data, num = u64(raw, 0), i32(raw, 8)
                block = read(data, min(num, 16) * 64) if is_pointer(data) and 0 < num <= 64 else None
                if block is not None:
                    say(f"      map elements ({num}): {hex_words(block)}")


def dump_libraries() -> None:
    say("step 5: function libraries")
    for name in LIBRARIES:
        try:
            cls = unrealsdk.find_class(name)
        except ValueError:
            say(f"   {name}: no such class")
            continue
        functions = []
        for field in cls._fields():
            if str(field.Class.Name) == "Function":
                params = ", ".join(f"{p.Name}:{str(p.Class.Name).replace('Property', '')}" for p in field._properties())
                functions.append(f"{field.Name}({params})")
        say(f"   {name}: {len(functions)} function(s)")
        for text in functions:
            say(f"      {text}")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v18: stat values, stats def, tables, globals")
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
    if weapon is None:
        say("no weapon in hand")
    else:
        identity = int(weapon.Item.data.Identity._get_address())
        head0 = u64(read(identity, 8), 0)
        type_struct = find_struct(TYPE_STRUCT)
        say(f"   gun: {views.view(type_struct, head0).ItemBaseName!s}")
        dump_stat_modifiers(views, identity)
        dump_stats_def(views, views.view(type_struct, head0))
        dump_tables(views, views.view(type_struct, head0), by_address)
        arena = Arena(head0)
        say(f"   arena at {arena.base:#x}: {arena.size / 1e6:.1f} MB")
        find_globals(arena, by_address, views)
    dump_libraries()
except Exception as ex:  # noqa: BLE001
    import traceback
    say(f"   failed: {type(ex).__name__}: {ex}")
    say(traceback.format_exc()[-1500:])
finally:
    if views is not None:
        views.restore()
say("done")
