"""The item name reader against a fake process memory: the three UI models, the scan for
the seed, the pairing of slots with cards, the confirming read, the retry policy."""

from __future__ import annotations

import struct
import unittest

from dmgtracker import itemnames
from dmgtracker.itemnames import (
    LATEST_LAYOUT,
    SCAN_CHUNK_BYTES,
    SCAN_RETRY_FIRST_S,
    BuildLayout,
    ItemNameReader,
    build_changelist,
    layout_for_build,
)

# Fake addresses, all above the reader's lowest plausible pointer: one heap block for the
# equipped models, one for the inventory list, one for the string buffers.
MODELS_BLOCK = 0x400000
MODELS_BLOCK_BYTES = 0x10000
LIST_BLOCK = 0x800000
STRINGS_BLOCK = 0xC00000
SEEDS = [bytes([index + 1] * 12) for index in range(4)]
GUNS = [
    ("assaultrifle", "legendary", "Domino Toochney Lucian's Flank", "@Ugvelk3C0d0", "corrosive", "911"),
    ("smg", "pearl", "Lionized Parasite", "@UgxFw!3FZzW", "radiation", "1.699"),
    ("pistol", "pearl", "Inoculated Performative Handcannon", "@Ugct)%Fme##", "noelement", "4.781"),
    ("shotgun", "legendary", "Maintained Screeching T.K's Wave", "@Ugd_t@Fme!K", "noelement", "1.612 x 10"),
]


class FakeMemory:
    """A sparse process memory: blocks of bytes at addresses, unreadable elsewhere."""

    def __init__(self) -> None:
        self.blocks: dict[int, bytearray] = {}

    def block(self, address: int, size: int) -> None:
        self.blocks[address] = bytearray(size)

    def write(self, address: int, data: bytes) -> None:
        """Write into an existing block, or create one holding exactly this data."""
        for start, block in self.blocks.items():
            if start <= address < start + len(block):
                block[address - start:address - start + len(data)] = data
                return
        self.blocks[address] = bytearray(data)

    def read(self, address: int, size: int) -> bytes | None:
        """Like ReadProcessMemory: the readable prefix, None when nothing is readable."""
        for start, block in self.blocks.items():
            if start <= address < start + len(block):
                return bytes(block[address - start:address - start + size])
        return None

    def regions(self) -> list[tuple[int, int]]:
        return sorted((start, len(block)) for start, block in self.blocks.items())


class StringHeap:
    """Lays FString buffers out one after the other and hands out their headers."""

    def __init__(self, memory: FakeMemory, base: int = STRINGS_BLOCK) -> None:
        self.memory = memory
        self.next = base

    def fstring(self, text: str) -> bytes:
        if text == "":
            return struct.pack("<QII", 0, 0, 0)
        raw = text.encode("utf-16-le") + b"\x00\x00"
        address = self.next
        self.memory.write(address, raw)
        self.next += len(raw) + 16
        return struct.pack("<QII", address, len(text) + 1, len(text) + 1)


def make_slot(heap: StringHeap, type_slug: str, rarity: str, seed: bytes, layout: BuildLayout = LATEST_LAYOUT) -> bytes:
    record = bytearray(layout.slots.record_bytes)
    record[layout.slots.type_offset:layout.slots.type_offset + 16] = heap.fstring(type_slug)
    record[layout.slots.rarity_offset:layout.slots.rarity_offset + 16] = heap.fstring(rarity)
    record[layout.slots.seed_offset:layout.slots.seed_offset + 12] = seed
    return bytes(record)


def make_card(heap: StringHeap, name: str, serial: str, rarity: str, element: str = "corrosive", damage: str = "911", slot_type: str = "weapon", layout: BuildLayout = LATEST_LAYOUT) -> bytes:
    record = bytearray(layout.cards.record_bytes)
    fields = {
        layout.cards.slot_offset: slot_type,
        layout.cards.name_offset: name + " ",
        layout.cards.element_offset: element,
        layout.cards.damage_offset: damage,
        layout.cards.serial_offset: serial,
        layout.cards.rarity_offset: rarity,
    }
    for offset, text in fields.items():
        record[offset:offset + 16] = heap.fstring(text)
    return bytes(record)


def make_list_record(heap: StringHeap, type_slug: str, rarity: str, name: str, serial: str, seed: bytes, element: str = "impact", slot_type: str = "weapon", layout: BuildLayout = LATEST_LAYOUT) -> bytes:
    record = bytearray(layout.inventory.record_bytes)
    fields = {
        layout.inventory.slot_offset: slot_type,
        layout.inventory.type_offset: type_slug,
        layout.inventory.name_offset: name + " ",
        layout.inventory.rarity_offset: rarity,
        layout.inventory.element_offset: element,
        layout.inventory.serial_offset: serial,
    }
    for offset, text in fields.items():
        record[offset:offset + 16] = heap.fstring(text)
    record[layout.inventory.seed_offset:layout.inventory.seed_offset + 12] = seed
    return bytes(record)


def write_models(memory: FakeMemory, heap: StringHeap, slots_at: int, cards_at: int, guns=GUNS, seeds=SEEDS, card_rarities: list[str] | None = None) -> None:
    """The slot records and the item cards of the four guns, in slot order."""
    for index, (type_slug, rarity, _name, _serial, _element, _damage) in enumerate(guns):
        memory.write(slots_at + index * LATEST_LAYOUT.slots.record_bytes, make_slot(heap, type_slug, rarity, seeds[index]))
    for index, (_type_slug, rarity, name, serial, element, damage) in enumerate(guns):
        card_rarity = card_rarities[index] if card_rarities else rarity
        memory.write(cards_at + index * LATEST_LAYOUT.cards.record_bytes, make_card(heap, name, serial, card_rarity, element, damage))


def write_list(memory: FakeMemory, heap: StringHeap, list_at: int, guns=GUNS, seeds=SEEDS) -> None:
    for index, (type_slug, rarity, name, serial, _element, _damage) in enumerate(guns):
        memory.write(list_at + index * LATEST_LAYOUT.inventory.record_bytes, make_list_record(heap, type_slug, rarity, name, serial, seeds[index]))


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ReaderHarness:
    """A reader over a fake memory whose scan thread can be waited for."""

    def __init__(self, memory: FakeMemory) -> None:
        self.memory = memory
        self.clock = FakeClock()
        self.reader = ItemNameReader(LATEST_LAYOUT, memory.read, memory.regions, self.clock)

    def names_after_scan(self, seeds: list[bytes]) -> dict[bytes, str]:
        """One call that starts the scan, the scan itself, and the call after it."""
        self.reader.names_for(seeds)
        self.wait_for_scan()
        return self.reader.names_for(seeds)

    def wait_for_scan(self) -> None:
        if self.reader._scan is not None:
            self.reader._scan.join(timeout=30.0)


NAMES_BY_SEED = {seed: gun[2] for seed, gun in zip(SEEDS, GUNS)}


class ItemNameReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = FakeMemory()
        self.memory.block(MODELS_BLOCK, MODELS_BLOCK_BYTES)
        self.heap = StringHeap(self.memory)

    def test_names_come_from_the_slot_records_and_the_cards_near_them(self) -> None:
        # Session 3 layout: the cards 9 KB before the slot records.
        write_models(self.memory, self.heap, MODELS_BLOCK + 0x3000, MODELS_BLOCK + 0x800)
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.reader.names_for(SEEDS), {})
        harness.wait_for_scan()
        # The scan's read is the first of the two that must agree; the next call confirms.
        self.assertEqual(harness.reader.names_for(SEEDS), NAMES_BY_SEED)
        self.assertEqual(harness.reader.scans, 1)

    def test_cards_right_after_the_slot_records_are_found_too(self) -> None:
        slots_at = MODELS_BLOCK + 0x1000
        write_models(self.memory, self.heap, slots_at, slots_at + 5 * LATEST_LAYOUT.slots.record_bytes + 8)
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.names_after_scan(SEEDS), NAMES_BY_SEED)

    def test_a_pairing_that_changes_between_the_reads_waits_for_a_steady_one(self) -> None:
        cards_at = MODELS_BLOCK + 0x800
        write_models(self.memory, self.heap, MODELS_BLOCK + 0x3000, cards_at)
        harness = ReaderHarness(self.memory)
        harness.reader.names_for(SEEDS)
        harness.wait_for_scan()
        # The UI lagged: card 1 changes before the confirming read.
        self.memory.write(cards_at + LATEST_LAYOUT.cards.record_bytes, make_card(self.heap, "Lionized Mercurious", "@UgxFw!2}TYg", "pearl"))
        names = harness.reader.names_for(SEEDS)
        self.assertNotIn(SEEDS[1], names)
        self.assertEqual(names[SEEDS[0]], GUNS[0][2])
        self.assertEqual(harness.reader.names_for(SEEDS)[SEEDS[1]], "Lionized Mercurious")

    def test_names_come_from_the_inventory_list_at_once(self) -> None:
        write_list(self.memory, self.heap, LIST_BLOCK)
        harness = ReaderHarness(self.memory)
        harness.reader.names_for(SEEDS[:1])
        harness.wait_for_scan()
        self.assertEqual(harness.reader.names_for(SEEDS), NAMES_BY_SEED)

    def test_a_gun_missing_from_a_stale_list_is_looked_for_again_later(self) -> None:
        write_list(self.memory, self.heap, LIST_BLOCK, GUNS[:3], SEEDS[:3])
        harness = ReaderHarness(self.memory)
        names = harness.names_after_scan(SEEDS)
        self.assertEqual(names, {seed: NAMES_BY_SEED[seed] for seed in SEEDS[:3]})
        # The list gave three names; the second call scans for the fourth gun and finds
        # nothing, so the next scan waits.
        harness.wait_for_scan()
        self.assertEqual(harness.reader.scans, 2)
        self.assertGreater(harness.reader._next_scan_at, harness.clock.now)
        self.assertIn(SEEDS[3].hex()[:6], harness.reader.last_error)
        harness.reader.names_for(SEEDS)
        self.assertEqual(harness.reader.scans, 2)

    def test_a_rarity_mismatch_pairs_nothing(self) -> None:
        write_models(self.memory, self.heap, MODELS_BLOCK + 0x3000, MODELS_BLOCK + 0x800, card_rarities=["legendary", "epic", "pearl", "legendary"])
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.names_after_scan(SEEDS), {})

    def test_the_card_array_that_pairs_with_the_slots_wins_over_a_nearer_stale_one(self) -> None:
        slots_at = MODELS_BLOCK + 0x6000
        write_models(self.memory, self.heap, slots_at, MODELS_BLOCK + 0x800)
        stale_guns = [(type_slug, "common", "Old " + name, serial, element, damage) for type_slug, _rarity, name, serial, element, damage in GUNS]
        for index, (_type_slug, rarity, name, serial, element, damage) in enumerate(stale_guns):
            self.memory.write(MODELS_BLOCK + 0x5000 + index * LATEST_LAYOUT.cards.record_bytes, make_card(self.heap, name, serial, rarity, element, damage))
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.names_after_scan(SEEDS), NAMES_BY_SEED)

    def test_a_seed_on_a_chunk_boundary_is_still_found(self) -> None:
        big_block = 0x2000000
        self.memory.block(big_block, SCAN_CHUNK_BYTES + 0x4000)
        # The seed's twelve bytes start 8 bytes before the chunk boundary.
        slots_at = big_block + SCAN_CHUNK_BYTES - LATEST_LAYOUT.slots.seed_offset - 8
        write_models(self.memory, self.heap, slots_at, slots_at + 4 * LATEST_LAYOUT.slots.record_bytes + 8)
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.names_after_scan(SEEDS[:1]), {SEEDS[0]: GUNS[0][2]})

    def test_the_window_around_the_slots_skips_unreadable_pages(self) -> None:
        # The slots at the very start of their block: the window before them is unmapped.
        slots_at = MODELS_BLOCK
        write_models(self.memory, self.heap, slots_at, slots_at + 0x2000)
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.names_after_scan(SEEDS), NAMES_BY_SEED)

    def test_a_new_gun_is_read_from_the_remembered_models_without_a_scan(self) -> None:
        slots_at, cards_at = MODELS_BLOCK + 0x3000, MODELS_BLOCK + 0x800
        write_models(self.memory, self.heap, slots_at, cards_at)
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.names_after_scan(SEEDS), NAMES_BY_SEED)
        new_seed = bytes([9] * 12)
        self.memory.write(slots_at + LATEST_LAYOUT.slots.record_bytes, make_slot(self.heap, "smg", "legendary", new_seed))
        self.memory.write(cards_at + LATEST_LAYOUT.cards.record_bytes, make_card(self.heap, "Plasma Coil", "@Ugw$Yw38oGr", "legendary"))
        self.assertEqual(harness.reader.names_for([new_seed]), {})
        self.assertEqual(harness.reader.names_for([new_seed]), {new_seed: "Plasma Coil"})
        self.assertEqual(harness.reader.scans, 1)

    def test_a_scan_that_finds_nothing_rests_longer_each_time(self) -> None:
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.names_after_scan(SEEDS[:1]), {})
        self.assertEqual(harness.reader.scans, 1)
        harness.reader.names_for(SEEDS[:1])
        self.assertEqual(harness.reader.scans, 1)
        harness.clock.now += SCAN_RETRY_FIRST_S + 1
        harness.reader.names_for(SEEDS[:1])
        harness.wait_for_scan()
        self.assertEqual(harness.reader.scans, 2)
        self.assertEqual(harness.reader._next_scan_at, harness.clock.now + 2 * SCAN_RETRY_FIRST_S)

    def test_forget_drops_the_names_and_starts_over(self) -> None:
        write_models(self.memory, self.heap, MODELS_BLOCK + 0x3000, MODELS_BLOCK + 0x800)
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.names_after_scan(SEEDS), NAMES_BY_SEED)
        harness.reader.forget()
        self.assertEqual(harness.reader.known, 0)
        self.assertEqual(harness.names_after_scan(SEEDS), NAMES_BY_SEED)
        self.assertEqual(harness.reader.scans, 2)

    def test_unreadable_name_text_fails_the_card(self) -> None:
        write_models(self.memory, self.heap, MODELS_BLOCK + 0x3000, MODELS_BLOCK + 0x800)
        broken = bytearray(make_card(self.heap, "Broken", "@Ug", "legendary"))
        broken[LATEST_LAYOUT.cards.name_offset:LATEST_LAYOUT.cards.name_offset + 16] = struct.pack("<QII", 0x7F0000, 8, 8)
        self.memory.write(MODELS_BLOCK + 0x800, bytes(broken))
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.names_after_scan(SEEDS), {})

    def test_seeds_of_the_wrong_length_are_ignored(self) -> None:
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.reader.names_for([b"short"]), {})
        self.assertIsNone(harness.reader._scan)


class BuildLayoutTest(unittest.TestCase):
    def test_changelist_parsing(self) -> None:
        self.assertEqual(build_changelist("Oak2-Win64-4845623-CL-4845623"), "4845623")
        self.assertEqual(build_changelist("Oak2-Win64-4845623"), "")
        self.assertEqual(build_changelist("Oak2-CL-abc"), "")

    def test_known_and_unknown_builds(self) -> None:
        layout, verified = layout_for_build("Oak2-Win64-4845623-CL-4845623")
        self.assertTrue(verified)
        self.assertEqual(layout.changelist, "4845623")
        layout, verified = layout_for_build("Oak2-Win64-9999999-CL-9999999")
        self.assertFalse(verified)
        self.assertIs(layout, itemnames.LATEST_LAYOUT)


if __name__ == "__main__":
    unittest.main()
