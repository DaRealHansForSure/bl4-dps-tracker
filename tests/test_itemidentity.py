"""The stable weapon identity: seed bytes read from the live item, with every guard."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from dmgtracker.itemidentity import (
    IDENTITY_SEED_BYTES,
    IDENTITY_SEED_OFFSET,
    ItemIdentityReader,
    instance_item_id,
    short_item_id,
)

WEAPON_ADDRESS = 0x1000
ITEM_OFFSET = 1000
DATA_OFFSET = 0
IDENTITY_OFFSET = 8
IDENTITY_SIZE = 216
SEED = bytes(range(1, IDENTITY_SEED_BYTES + 1))


def fake_property(name: str, offset: int, struct=None) -> SimpleNamespace:
    return SimpleNamespace(Name=name, Offset_Internal=offset, Struct=struct)


def fake_weapon(instance_id: int = 277, identity_size: int = IDENTITY_SIZE, identity_address: int | None = None, class_name: str = "OakWeapon"):
    """Mimics the reflection the reader uses: the class's property chain Item -> data -> Identity,
    the object's address and the wrapped identity struct's address."""
    identity_struct = SimpleNamespace(_get_struct_size=lambda: identity_size)
    data_struct = SimpleNamespace(_properties=lambda: [fake_property("InstanceId", 0), fake_property("Identity", IDENTITY_OFFSET, identity_struct)])
    item_struct = SimpleNamespace(_properties=lambda: [fake_property("data", DATA_OFFSET, data_struct)])
    weapon_class = SimpleNamespace(Name=class_name, _properties=lambda: [fake_property("Item", ITEM_OFFSET, item_struct)])
    if identity_address is None:
        identity_address = WEAPON_ADDRESS + ITEM_OFFSET + DATA_OFFSET + IDENTITY_OFFSET
    identity = SimpleNamespace(_get_address=lambda: identity_address)
    return SimpleNamespace(
        Class=weapon_class,
        _get_address=lambda: WEAPON_ADDRESS,
        Item=SimpleNamespace(data=SimpleNamespace(InstanceId=instance_id, Identity=identity)),
    )


class FakeMemory:
    """Serves bytes for one address range and records every read."""

    def __init__(self, seed: bytes = SEED) -> None:
        self.seed = seed
        self.reads: list[tuple[int, int]] = []

    def __call__(self, address: int, count: int) -> bytes:
        self.reads.append((address, count))
        return self.seed[:count]


class ItemIdentityReaderTests(unittest.TestCase):
    def test_reads_the_seed_bytes_inside_the_identity_struct(self) -> None:
        memory = FakeMemory()
        reader = ItemIdentityReader(memory)
        self.assertEqual(reader.item_id(fake_weapon()), SEED.hex())
        expected_address = WEAPON_ADDRESS + ITEM_OFFSET + DATA_OFFSET + IDENTITY_OFFSET + IDENTITY_SEED_OFFSET
        self.assertEqual(memory.reads, [(expected_address, IDENTITY_SEED_BYTES)])
        self.assertEqual(reader.fallbacks, 0)

    def test_address_mismatch_falls_back_without_reading(self) -> None:
        memory = FakeMemory()
        reader = ItemIdentityReader(memory)
        self.assertEqual(reader.item_id(fake_weapon(277, identity_address=0x9999)), "instance:277")
        self.assertEqual(memory.reads, [])
        self.assertEqual(reader.fallbacks, 1)
        self.assertIn("mismatch", reader.last_error)

    def test_struct_too_small_for_the_seed_falls_back(self) -> None:
        memory = FakeMemory()
        reader = ItemIdentityReader(memory)
        self.assertEqual(reader.item_id(fake_weapon(5, identity_size=100)), "instance:5")
        self.assertEqual(memory.reads, [])
        self.assertIn("100 bytes", reader.last_error)

    def test_missing_reflection_falls_back(self) -> None:
        reader = ItemIdentityReader(FakeMemory())
        weapon = SimpleNamespace(Class=SimpleNamespace(Name="Odd", _properties=lambda: []), Item=SimpleNamespace(data=SimpleNamespace(InstanceId=9)))
        self.assertEqual(reader.item_id(weapon), "instance:9")
        self.assertIn("no property Item", reader.last_error)

    def test_all_zero_seed_is_not_an_identity(self) -> None:
        reader = ItemIdentityReader(FakeMemory(bytes(IDENTITY_SEED_BYTES)))
        self.assertEqual(reader.item_id(fake_weapon(3)), "instance:3")

    def test_read_errors_never_escape(self) -> None:
        def broken(address: int, count: int) -> bytes:
            raise OSError("no")

        reader = ItemIdentityReader(broken)
        self.assertEqual(reader.item_id(fake_weapon(4)), "instance:4")
        self.assertEqual(reader.last_error, "OSError: no")

    def test_layout_is_resolved_once_per_class(self) -> None:
        memory = FakeMemory()
        reader = ItemIdentityReader(memory)
        first = fake_weapon(1)
        calls = {"count": 0}
        original = first.Class._properties

        def counting() -> list:
            calls["count"] += 1
            return original()

        first.Class._properties = counting
        reader.item_id(first)
        reader.item_id(first)
        self.assertEqual(calls["count"], 1)


class HelperTests(unittest.TestCase):
    def test_instance_item_id(self) -> None:
        self.assertEqual(instance_item_id(fake_weapon(280)), "instance:280")

    def test_short_item_id(self) -> None:
        self.assertEqual(short_item_id("instance:280"), "#280")
        self.assertEqual(short_item_id("277"), "#277")
        self.assertEqual(short_item_id("97777d4239ef3f571e0637cf"), "97777d")


if __name__ == "__main__":
    unittest.main()
