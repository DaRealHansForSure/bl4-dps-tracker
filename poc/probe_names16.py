"""
Phase 0 probe #31: the weapon naming strategy in full.

probe_names15 found it: `InventoryNamingStrategyDef.NamingStrategy` is an
`OakWeaponNamingStrategy` with NamingAttributeThresholds, SingleNames, DoubleNames,
CombinationNames and two data tables, so weapon prefixes come from attribute thresholds,
not from parts. This probe prints the layouts of the strategy structs and dumps every entry
through the typed view (the SDK handles the inline struct), then the two data tables.

    pyexec probe_names16.py    with any gun in hand

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
STRATEGY_STRUCT = "/Script/GbxGame.InventoryNamingStrategyDef"
SCRATCH_STRUCT = "InventoryTypeDef"
SCRATCH_FIELD = "BaseType"
MAX_VALUE_CHARS = 2000
MAX_ITEMS = 80
LAYOUTS = (
    "/Script/OakGame.OakWeaponNamingStrategy",
    "/Script/OakGame.OakWeaponNamingAttributeThresholdData",
    "/Script/OakGame.OakWeaponNamingAttributePair",
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
    logging.info(f"[dmgnames16] {msg}")
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
    """Name, type and address of a def pointer without the SDK's recursive repr."""
    address = 0
    try:
        address = int(value._get_address())
    except Exception:  # noqa: BLE001
        pass
    extras = []
    for attribute in ("name", "_name", "type", "_type", "def_name", "type_name"):
        try:
            found = getattr(value, attribute)
        except Exception:  # noqa: BLE001
            continue
        try:
            extras.append(f"{attribute}={str(found.Name) if hasattr(found, 'Name') else found}")
        except Exception:  # noqa: BLE001
            pass
    return f"<def {address:#x} {' '.join(extras)}>"


def described(value: Any, depth: int = 0) -> str:
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
    say(f"   {path} ({struct._get_struct_size()} bytes)")
    for prop in struct._properties():
        kind = str(prop.Class.Name).replace("Property", "")
        detail = ""
        for attribute in ("Inner", "Struct", "PropertyClass", "Enum"):
            try:
                target = getattr(prop, attribute)
            except Exception:  # noqa: BLE001
                continue
            if target is None:
                continue
            try:
                detail += f" {attribute}={target.Name}" if hasattr(target, "Name") else ""
                inner_struct = getattr(target, "Struct", None) if attribute == "Inner" else None
                if inner_struct is not None:
                    detail += f"<{inner_struct.Name}>"
            except Exception:  # noqa: BLE001
                pass
        say(f"      {prop.Name}@{int(prop.Offset_Internal)} {kind} {int(prop.ElementSize)}B{detail}")


def dump_table(label: str, pointer: Any, by_address: dict[int, Any], views: DefView) -> None:
    address = 0
    try:
        address = int(pointer._get_address())
    except Exception:  # noqa: BLE001
        pass
    say(f"   {label}: {def_label(pointer)}")
    if not address:
        return
    # The triple that holds it lives in the strategy; we only know the data. Try the
    # def header before the data for its heap record, and dump the data as raw words.
    header = read(address - 0x18, 0x18)
    raw = read(address, 128)
    if header is not None:
        say(f"      header {header.hex()}")
    if raw is not None:
        say("      data " + " ".join(raw[i:i + 8].hex() for i in range(0, 128, 8)))
        for offset in range(0, 128 - 7, 8):
            struct = by_address.get(u64(raw, offset))
            if struct is not None:
                say(f"      +{offset}: struct {struct._path_name()}")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v16: the weapon naming strategy")
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
    say("step 1: layouts")
    for path in LAYOUTS:
        describe_layout(path)
    views = DefView()
    if weapon is None:
        say("no weapon in hand")
    else:
        identity = int(weapon.Item.data.Identity._get_address())
        head0 = u64(read(identity, 8), 0)
        type_view = views.view(find_struct(TYPE_STRUCT), head0)
        say(f"   gun: {type_view.ItemBaseName!s}")
        strategy_address = int(type_view.NamingStrategyDef._get_address())
        say(f"step 2: the strategy at {strategy_address:#x}")
        strategy = views.view(find_struct(STRATEGY_STRUCT), strategy_address).NamingStrategy
        say(f"   dir(def pointer): {[n for n in dir(type_view.NamingStrategyDef) if not n.startswith('__')]}")
        for field in ("NamingAttributeThresholds", "SingleNames", "DoubleNames", "CombinationNames"):
            try:
                entries = list(getattr(strategy, field))
            except Exception as ex:  # noqa: BLE001
                say(f"   {field}: <err {type(ex).__name__}: {ex}>")
                continue
            say(f"   {field}: {len(entries)} entries")
            for index, entry in enumerate(entries):
                say(f"      [{index}] {described(entry)}")
        for field in ("PayloadDeliveryDataTable", "MagBarrelDataTable"):
            try:
                dump_table(field, getattr(strategy, field), by_address, views)
            except Exception as ex:  # noqa: BLE001
                say(f"   {field}: <err {type(ex).__name__}: {ex}>")
except Exception as ex:  # noqa: BLE001
    import traceback
    say(f"   failed: {type(ex).__name__}: {ex}")
    say(traceback.format_exc()[-1500:])
finally:
    if views is not None:
        views.restore()
say("done")
