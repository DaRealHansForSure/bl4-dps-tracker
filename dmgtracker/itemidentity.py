"""
A weapon identity that survives restarts: the seed bytes inside the item's InventoryIdentity.

`Item.data.InstanceId` is a process-wide counter (poc/findings.md): it repeats across
launches that continue with the same character and shifts once another character was loaded
first. The `InventoryIdentity` struct (216 bytes, no reflected fields) carries two
high-entropy values at bytes 200-211 that stayed the same for every gun across two launches
and differ between guns (poc/probe_identity.py, 2026-09-20); those twelve bytes, as hex, are
the item id the records are keyed on.

They are read raw, through ctypes, at the struct's address inside the live weapon object.
Safety rules: the read happens only after two independent computations of that address
agree (the struct wrapper's own address, and the object address plus the reflected property
offsets) and only when the reflected struct size covers the bytes. Otherwise, and on any
error, the weapon gets the instance id as a fallback identity, which still works within one
launch. Nothing here raises into the caller. No SDK imports: the objects are duck-typed, so
the tests use fakes.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import Any, Callable

IDENTITY_SEED_OFFSET = 200
IDENTITY_SEED_BYTES = 12
ITEM_PROPERTY = "Item"
DATA_PROPERTY = "data"
IDENTITY_PROPERTY = "Identity"
INSTANCE_PREFIX = "instance:"
SHORT_ID_CHARS = 6

MemoryReader = Callable[[int, int], bytes]


def instance_item_id(weapon: Any) -> str:
    """The fallback identity: the process-wide instance id, marked as such."""
    return f"{INSTANCE_PREFIX}{int(weapon.Item.data.InstanceId)}"


def short_item_id(item_id: str) -> str:
    """A few characters to tell two guns of the same label apart in console output:
    '#277' for instance ids (and the plain numbers of older logs), the first hex digits of
    a seed otherwise."""
    if item_id.startswith(INSTANCE_PREFIX):
        return "#" + item_id[len(INSTANCE_PREFIX) :]
    if item_id.isdigit():
        return "#" + item_id
    return item_id[:SHORT_ID_CHARS]


@dataclass(frozen=True)
class IdentityLayout:
    """Where the identity struct sits inside a weapon object, from the reflected offsets."""

    offset: int
    size: int


class ItemIdentityReader:
    """Reads the seed bytes of weapons; counts how often it had to fall back and why."""

    def __init__(self, read_memory: MemoryReader = ctypes.string_at) -> None:
        self._read_memory = read_memory
        self._layout_by_class: dict[str, IdentityLayout | None] = {}
        self.fallbacks = 0
        self.last_error = ""

    def item_id(self, weapon: Any) -> str:
        try:
            seed = self._read_seed(weapon)
        except Exception as ex:  # noqa: BLE001 - a failed read costs identity, never the game
            seed = None
            self.last_error = f"{type(ex).__name__}: {ex}"
        if seed is None or not any(seed):
            self.fallbacks += 1
            return instance_item_id(weapon)
        return seed.hex()

    def _read_seed(self, weapon: Any) -> bytes | None:
        layout = self._layout_for(weapon)
        if layout is None:
            return None
        computed = int(weapon._get_address()) + layout.offset
        reported = int(weapon.Item.data.Identity._get_address())
        if computed != reported:
            self.last_error = f"identity address mismatch: computed {computed:#x}, wrapper {reported:#x}"
            return None
        return self._read_memory(computed + IDENTITY_SEED_OFFSET, IDENTITY_SEED_BYTES)

    def _layout_for(self, weapon: Any) -> IdentityLayout | None:
        """The offsets are read once per weapon class; a class whose layout does not fit
        stays on the fallback for the whole launch."""
        class_name = str(weapon.Class.Name)
        if class_name in self._layout_by_class:
            return self._layout_by_class[class_name]
        layout: IdentityLayout | None = None
        try:
            item_property = _property_named(weapon.Class, ITEM_PROPERTY)
            data_property = _property_named(item_property.Struct, DATA_PROPERTY)
            identity_property = _property_named(data_property.Struct, IDENTITY_PROPERTY)
            size = int(identity_property.Struct._get_struct_size())
            offset = int(item_property.Offset_Internal) + int(data_property.Offset_Internal) + int(identity_property.Offset_Internal)
            if size >= IDENTITY_SEED_OFFSET + IDENTITY_SEED_BYTES:
                layout = IdentityLayout(offset, size)
            else:
                self.last_error = f"InventoryIdentity is {size} bytes, the seed needs {IDENTITY_SEED_OFFSET + IDENTITY_SEED_BYTES}"
        except Exception as ex:  # noqa: BLE001
            self.last_error = f"{type(ex).__name__}: {ex}"
        self._layout_by_class[class_name] = layout
        return layout


def _property_named(owner: Any, name: str) -> Any:
    """A property of a class or script struct by name, inherited ones included."""
    for candidate in owner._properties():
        if str(candidate.Name) == name:
            return candidate
    raise KeyError(f"no property {name}")
