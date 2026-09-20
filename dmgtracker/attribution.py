"""
Which source dealt a damage instance, and what to call it.

BL4 exposes no display name for a weapon through reflection (see poc/findings.md, section B),
so a weapon is labelled by manufacturer and type, both read from the stem of its BodyData
asset path (`Body_JAK_SR` -> "Jakobs Sniper"), and identified by its item InstanceId.

This module never imports the SDK. The resolver only touches attributes of the objects it is
handed, which keeps it testable with plain fakes. Grenade and skill attribution, when they
come, are new resolver branches here; the poller does not change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SOURCE_WEAPON = "weapon"
SOURCE_UNKNOWN = "unknown"
UNKNOWN_LABEL = "Unknown"
WEAPON_CLASS_NAME = "OakWeapon"
MAX_CACHED_LABELS = 256

# Manufacturer codes as they appear in the body asset names. Unknown codes fall back to the
# code itself, so a wrong or missing entry only costs readability, never data.
MANUFACTURER_NAMES = {
    "VLA": "Vladof",
    "JAK": "Jakobs",
    "TOR": "Torgue",
    "MAL": "Maliwan",
    "TED": "Tediore",
    "BOR": "Bor",
    "DAL": "Daedalus",
    "RIP": "Ripper",
    "ORD": "Order",
}

WEAPON_TYPE_NAMES = {
    "AR": "Assault Rifle",
    "SM": "SMG",
    "SR": "Sniper",
    "SG": "Shotgun",
    "PS": "Pistol",
    "HW": "Heavy",
}

BODY_STEM_PATTERN = re.compile(r"Body_([A-Za-z0-9]+)_([A-Za-z0-9]+)$")


@dataclass(frozen=True)
class DamageSource:
    """What dealt the damage: its kind, a display label, and enough raw data to relabel later."""

    kind: str
    label: str
    body: str
    instance_id: int


UNKNOWN_SOURCE = DamageSource(SOURCE_UNKNOWN, UNKNOWN_LABEL, "", 0)


def body_stem(body_path: str) -> str:
    """'/Game/Gear/Weapons/Sniper/JAK/Body_JAK_SR.Body_JAK_SR' -> 'Body_JAK_SR'."""
    object_name = body_path.rsplit(".", 1)[-1]
    return object_name.rsplit("/", 1)[-1]


def label_for_body(stem: str) -> str:
    """Human label for a body stem: 'Body_JAK_SR' -> 'Jakobs Sniper'. Unknown codes are kept as
    they are, an unparsable stem is returned unchanged, an empty one becomes 'Unknown'."""
    if stem == "":
        return UNKNOWN_LABEL
    match = BODY_STEM_PATTERN.search(stem)
    if match is None:
        return stem
    manufacturer_code = match.group(1).upper()
    type_code = match.group(2).upper()
    manufacturer = MANUFACTURER_NAMES.get(manufacturer_code, manufacturer_code)
    weapon_type = WEAPON_TYPE_NAMES.get(type_code, type_code)
    return f"{manufacturer} {weapon_type}"


class WeaponResolver:
    """Turns OakWeapon actors into DamageSources, caching the label per weapon instance so the
    hot path does one dict lookup per hit instead of a path-name read."""

    def __init__(self) -> None:
        self._sources_by_instance: dict[int, DamageSource] = {}

    def resolve_weapon(self, weapon: Any) -> DamageSource:
        """The DamageSource for one OakWeapon actor."""
        instance_id = int(weapon.Item.data.InstanceId)
        cached = self._sources_by_instance.get(instance_id)
        if cached is not None:
            return cached
        try:
            stem = body_stem(str(weapon.BodyData._path_name()))
        except Exception:  # noqa: BLE001 - a weapon without body data is still a weapon
            stem = ""
        source = DamageSource(SOURCE_WEAPON, label_for_body(stem), stem, instance_id)
        if len(self._sources_by_instance) >= MAX_CACHED_LABELS:
            self._sources_by_instance.clear()
        self._sources_by_instance[instance_id] = source
        return source

    def weapon_in_hand(self, pawn: Any) -> DamageSource:
        """The weapon the pawn holds right now, or UNKNOWN_SOURCE when there is none (no pawn,
        or the weapon structures are not filled yet right after a spawn)."""
        try:
            weapon = pawn.ActiveWeapons.Slots[0].Weapon
        except Exception:  # noqa: BLE001 - any failure here means "no weapon in hand"
            return UNKNOWN_SOURCE
        if weapon is None:
            return UNKNOWN_SOURCE
        return self.resolve_weapon(weapon)

    def equipped_weapons(self, pawn: Any) -> list[DamageSource]:
        """The weapons in the equipment slots, in slot order. Shield, grenade and repair kit
        share the same slot array and are skipped by class name."""
        sources: list[DamageSource] = []
        for slot in pawn.EquippedInventorySlots.items:
            item = slot.InstancedInventory
            if item is None or str(item.Class.Name) != WEAPON_CLASS_NAME:
                continue
            sources.append(self.resolve_weapon(item))
        return sources
