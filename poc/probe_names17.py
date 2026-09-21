"""
Phase 0 probe #32: the gun's parts typed, and the prefix data tables.

probe_names16 read the weapon naming strategy: a prefix from attribute thresholds (damage,
critdamage, firerate, reloadspeed, magsize, Accuracy, elementalpower, adsproficiency, with
single / double / combination words such as "Screeching" = damage + firerate), a prefix
from the manufacturer's licensed-part table (JAK_Weapon_LicensedPartPrefix_Table) and a
Tediore payload table. The SDK also types Gbx inline structs, so the parts' aspects can be
read as real structs now. This probe:

1. reads every part of the gun through the typed view: gestalt names, description, and
   every aspect with its type and fields (the stat modifiers are what the thresholds see);
2. prints the GbxUEDataTableDef layout and finds the two data tables as UObjects, then
   reads their rows (RowMap parsed raw, each row viewed as the table's RowStruct);
3. lists every other *LicensedPartPrefix* / *PayloadPrefix* data table with its rows.

    pyexec probe_names17.py    with the shotgun in hand

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
DEF_PTR_SIZE = 24
TYPE_STRUCT = "/Script/GbxGame.InventoryTypeDef"
PART_STRUCT = "/Script/GbxGame.InventoryPartDef"
SCRATCH_STRUCT = "InventoryTypeDef"
SCRATCH_FIELD = "BaseType"
MAX_VALUE_CHARS = 1500
MAX_ITEMS = 48
MAX_ROWS = 64
TABLE_NAME_MARKERS = ("LicensedPartPrefix", "PayloadPrefix")
QUIET_ASPECT_FIELDS = ("SelectionPriority", "params", "ComparisionTag")


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
    logging.info(f"[dmgnames17] {msg}")
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


def def_label(value: Any) -> str:
    address = 0
    try:
        address = int(value._get_address())
    except Exception:  # noqa: BLE001
        pass
    name = kind = None
    try:
        name = value._name
        kind = value._type
    except Exception:  # noqa: BLE001
        pass
    kind_name = None
    try:
        kind_name = str(kind.Name) if kind is not None else None
    except Exception:  # noqa: BLE001
        kind_name = str(kind)
    return f"<def {name} {kind_name} {address:#x}>"


def described(value: Any, depth: int = 0, quiet: tuple[str, ...] = ()) -> str:
    try:
        if isinstance(value, UObject):
            return f"{value.Class.Name}'{value._path_name()}'"
        if isinstance(value, FGbxDefPtr):
            return def_label(value)
        if isinstance(value, WrappedStruct):
            if depth >= 4:
                return f"<struct {value._type.Name}>"
            parts = []
            for prop in value._type._properties():
                name = str(prop.Name)
                if name in quiet:
                    continue
                try:
                    parts.append(f"{name}={described(getattr(value, name), depth + 1, quiet)}")
                except Exception as ex:  # noqa: BLE001
                    parts.append(f"{name}=<err {type(ex).__name__}>")
            return "{" + ", ".join(parts) + "}"
        if isinstance(value, WrappedArray):
            items = [described(item, depth + 1, quiet) for item in list(value)[:MAX_ITEMS]]
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


def describe_layout(path: str) -> None:
    try:
        struct = find_struct(path)
    except ValueError:
        say(f"   {path}: not found")
        return
    say(f"   {path} ({struct._get_struct_size()} bytes): " + ", ".join(f"{p.Name}@{int(p.Offset_Internal)}:{str(p.Class.Name).replace('Property', '')}" for p in struct._properties()))


def dump_parts(views: DefView, identity: int) -> None:
    say("step 1: the gun's parts, typed")
    part_struct = find_struct(PART_STRUCT)
    header = read(identity + 16, 16)
    if header is None:
        say("   parts array unreadable")
        return
    data, num = u64(header, 0), i32(header, 8)
    raw = read(data, num * 8) if 0 < num <= 64 and is_pointer(data) else None
    if raw is None:
        say("   parts unreadable")
        return
    for index in range(num):
        address = u64(raw, index * 8)
        if not readable(address, 128):
            say(f"   [{index}] {address:#x} unreadable")
            continue
        part = views.view(part_struct, address)
        try:
            say(f"   [{index}] {address:#x}: gestalt {described(part.GestaltPartNames)} description {described(part.DebugDisplayDescription)} params {described(part.params)}")
        except Exception as ex:  # noqa: BLE001
            say(f"   [{index}] {address:#x}: <err {type(ex).__name__}: {ex}>")
            continue
        try:
            aspects = list(part.Aspects)
        except Exception as ex:  # noqa: BLE001
            say(f"      aspects: <err {type(ex).__name__}: {ex}>")
            continue
        for aspect_index, aspect in enumerate(aspects):
            try:
                type_name = str(aspect._type.Name)
            except Exception:  # noqa: BLE001
                type_name = type(aspect).__name__
            say(f"      aspect {aspect_index} {type_name}: {described(aspect, 1, QUIET_ASPECT_FIELDS)}")


def table_rows(views: DefView, table: UObject) -> None:
    """Rows of a UDataTable: RowMap parsed raw right after the RowStruct property."""
    row_struct = table.RowStruct
    if row_struct is None:
        say("      no row struct")
        return
    say(f"      row struct {row_struct._path_name()} ({row_struct._get_struct_size()} bytes): " + ", ".join(f"{p.Name}:{str(p.Class.Name).replace('Property', '')}" for p in row_struct._properties()))
    row_struct_prop = next(p for p in table.Class._properties() if str(p.Name) == "RowStruct")
    map_address = int(table._get_address()) + int(row_struct_prop.Offset_Internal) + 8
    header = read(map_address, 16)
    if header is None:
        say("      row map unreadable")
        return
    data, num = u64(header, 0), i32(header, 8)
    say(f"      row map: data {data:#x} num {num}")
    if not is_pointer(data) or num <= 0 or num > 4096:
        return
    raw = read(data, min(num, MAX_ROWS) * 24)
    if raw is None:
        say("      row elements unreadable")
        return
    scratch = unrealsdk.make_struct("InventoryPartCriterionData")
    name_offset = int(next(p for p in scratch._type._properties() if str(p.Class.Name) == "NameProperty").Offset_Internal)
    for index in range(min(num, MAX_ROWS)):
        element = raw[index * 24:(index + 1) * 24]
        ctypes.memmove(int(scratch._get_address()) + name_offset, element[:8], 8)
        try:
            row_name = str(scratch.Name)
        except Exception:  # noqa: BLE001
            row_name = element[:8].hex()
        row_address = u64(element, 8)
        if not readable(row_address, 16):
            say(f"      row {row_name!r}: data {row_address:#x} unreadable")
            continue
        row = views.view(row_struct, row_address)
        say(f"      row {row_name!r}: {described(row)}")


def dump_tables(views: DefView, wanted: tuple[str, ...]) -> None:
    say("step 2: the prefix data tables")
    describe_layout("/Script/GbxEngine.GbxUEDataTableDef")
    describe_layout("/Script/GbxNexus.GbxUEDataTableDef")
    found = 0
    for table in unrealsdk.find_all("DataTable", exact=False):
        name = str(table.Name)
        if name.startswith("Default__"):
            continue
        if name in wanted or any(marker in name for marker in TABLE_NAME_MARKERS):
            found += 1
            say(f"   {table._path_name()}")
            try:
                table_rows(views, table)
            except Exception as ex:  # noqa: BLE001
                say(f"      <err {type(ex).__name__}: {ex}>")
    say(f"   {found} table(s)")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v17: parts typed, prefix tables")
pc = get_pc()
pawn = pc.Pawn if pc is not None else None
weapon = None
try:
    weapon = pawn.ActiveWeapons.Slots[0].Weapon if pawn is not None else None
except Exception:  # noqa: BLE001
    weapon = None
views: DefView | None = None
try:
    views = DefView()
    if weapon is None:
        say("no weapon in hand")
    else:
        identity = int(weapon.Item.data.Identity._get_address())
        head0 = u64(read(identity, 8), 0)
        type_view = views.view(find_struct(TYPE_STRUCT), head0)
        say(f"   gun: {type_view.ItemBaseName!s}; body {weapon.BodyData._path_name()}")
        dump_parts(views, identity)
    dump_tables(views, ("JAK_Weapon_LicensedPartPrefix_Table", "TED_PayloadPrefix_Table"))
except Exception as ex:  # noqa: BLE001
    import traceback
    say(f"   failed: {type(ex).__name__}: {ex}")
    say(traceback.format_exc()[-1500:])
finally:
    if views is not None:
        views.restore()
say("done")
