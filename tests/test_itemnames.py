"""The item name reader against a fake process memory: the cards' node and the inventory
list, the one-pass scan, the pairing by slot order, the confirming read, the retry policy."""

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
# cards' node and records, one for the inventory list, one for the string buffers.
MODELS_BLOCK = 0x400000
MODELS_BLOCK_BYTES = 0x10000
NODE_AT = MODELS_BLOCK + 0x3000
CARDS_AT = MODELS_BLOCK + 0x800
LIST_BLOCK = 0x800000
STRINGS_BLOCK = 0xC00000
SEEDS = [bytes([index + 1] * 12) for index in range(4)]
GUNS = [
    ("assaultrifle", "legendary", "Domino Toochney Lucian's Flank", "@Ugvelk3C0d0", "corrosive", "911"),
    ("smg", "pearl", "Lionized Parasite", "@UgxFw!3FZzW", "radiation", "1.699"),
    ("pistol", "pearl", "Inoculated Performative Handcannon", "@Ugct)%Fme##", "noelement", "4.781"),
    ("shotgun", "legendary", "Maintained Screeching T.K's Wave", "@Ugd_t@Fme!K", "noelement", "1.612 x 10"),
]
NAMES_BY_SEED = {seed: gun[2] for seed, gun in zip(SEEDS, GUNS)}


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


def make_node(cards_at: int, count: int, capacity: int | None = None, tail: bytes | None = None) -> bytes:
    """The 64-byte node around the cards' array header, as seen live: an unrelated
    one-element array header, {data, num, max}, two ints, a zero, a minus one."""
    node_layout = LATEST_LAYOUT.cards_node
    capacity = count if capacity is None else capacity
    tail = node_layout.after if tail is None else tail
    return struct.pack("<Q", 0x14B99E490) + node_layout.before + struct.pack("<QII", cards_at, count, capacity) + struct.pack("<QQ", 0x29, 0xD) + tail


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


def write_cards(memory: FakeMemory, heap: StringHeap, node_at: int = NODE_AT, cards_at: int = CARDS_AT, guns=GUNS, capacity: int | None = None) -> None:
    """The cards of the guns in slot order and the node that names their array; the node
    address is where the preceding one-element header starts."""
    for index, (_type_slug, rarity, name, serial, element, damage) in enumerate(guns):
        memory.write(cards_at + index * LATEST_LAYOUT.cards.record_bytes, make_card(heap, name, serial, rarity, element, damage))
    memory.write(node_at, make_node(cards_at, len(guns), capacity))


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
        self.log: list[str] = []
        self.reader = ItemNameReader(LATEST_LAYOUT, memory.read, memory.regions, self.clock, self.log.append)

    def names_after_scan(self, seeds: list[bytes | None]) -> dict[bytes, str]:
        """One call that starts the scan, the scan itself, and the call after it."""
        self.reader.names_for(seeds)
        self.wait_for_scan()
        return self.reader.names_for(seeds)

    def wait_for_scan(self) -> None:
        if self.reader._scan is not None:
            self.reader._scan.join(timeout=30.0)


class ItemNameReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = FakeMemory()
        self.memory.block(MODELS_BLOCK, MODELS_BLOCK_BYTES)
        self.heap = StringHeap(self.memory)

    def test_names_come_from_the_cards_found_through_their_node(self) -> None:
        write_cards(self.memory, self.heap)
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.reader.names_for(SEEDS), {})
        harness.wait_for_scan()
        # The scan's read is the first of the two that must agree; the next call confirms.
        self.assertEqual(harness.reader.names_for(SEEDS), NAMES_BY_SEED)
        self.assertEqual(harness.reader.scans, 1)
        self.assertIn("item cards at", harness.log[-1])

    def test_a_node_with_the_wrong_tail_or_count_is_not_the_cards(self) -> None:
        for index, (_type_slug, rarity, name, serial, element, damage) in enumerate(GUNS):
            self.memory.write(CARDS_AT + index * LATEST_LAYOUT.cards.record_bytes, make_card(self.heap, name, serial, rarity, element, damage))
        self.memory.write(NODE_AT, make_node(CARDS_AT, 4, tail=bytes(16)))
        self.memory.write(NODE_AT + 0x100, make_node(CARDS_AT, 4, capacity=40))
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.names_after_scan(SEEDS), {})
        self.assertIn("no item cards were found", harness.reader.last_error)

    def test_a_pairing_that_changes_between_the_reads_waits_for_a_steady_one(self) -> None:
        write_cards(self.memory, self.heap)
        harness = ReaderHarness(self.memory)
        harness.reader.names_for(SEEDS)
        harness.wait_for_scan()
        # The UI lagged: card 1 changes before the confirming read.
        self.memory.write(CARDS_AT + LATEST_LAYOUT.cards.record_bytes, make_card(self.heap, "Lionized Mercurious", "@UgxFw!2}TYg", "pearl"))
        names = harness.reader.names_for(SEEDS)
        self.assertNotIn(SEEDS[1], names)
        self.assertEqual(names[SEEDS[0]], GUNS[0][2])
        self.assertEqual(harness.reader.names_for(SEEDS)[SEEDS[1]], "Lionized Mercurious")

    def test_a_card_count_that_differs_from_the_slots_pairs_nothing(self) -> None:
        write_cards(self.memory, self.heap, guns=GUNS[:3])
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.names_after_scan(SEEDS), {})
        # With three slots the remembered cards pair; the second read confirms.
        harness.reader.names_for(SEEDS[:3])
        self.assertEqual(harness.reader.names_for(SEEDS[:3]), {seed: NAMES_BY_SEED[seed] for seed in SEEDS[:3]})

    def test_an_empty_slot_is_skipped_on_both_sides(self) -> None:
        # Four card records, the second one blank: the game's slot list skips that slot too.
        guns = [GUNS[0], ("smg", "", "", "", "", ""), GUNS[2], GUNS[3]]
        write_cards(self.memory, self.heap, guns=guns)
        harness = ReaderHarness(self.memory)
        slots = [SEEDS[0], SEEDS[2], SEEDS[3]]
        self.assertEqual(harness.names_after_scan(slots), {seed: NAMES_BY_SEED[seed] for seed in slots})

    def test_a_gun_without_a_seed_keeps_its_slot_in_the_pairing(self) -> None:
        write_cards(self.memory, self.heap)
        harness = ReaderHarness(self.memory)
        slots = [SEEDS[0], None, SEEDS[2], SEEDS[3]]
        self.assertEqual(harness.names_after_scan(slots), {seed: NAMES_BY_SEED[seed] for seed in (SEEDS[0], SEEDS[2], SEEDS[3])})

    def test_names_come_from_the_inventory_list_at_once(self) -> None:
        write_list(self.memory, self.heap, LIST_BLOCK)
        harness = ReaderHarness(self.memory)
        harness.reader.names_for(SEEDS[:1])
        harness.wait_for_scan()
        self.assertEqual(harness.reader.names_for(SEEDS), NAMES_BY_SEED)

    def test_items_that_are_not_weapons_get_a_scan_for_the_list_alone(self) -> None:
        # The cards are there (found first by a weapon scan), the grenade is only in the list.
        write_cards(self.memory, self.heap)
        grenade_seed = bytes([7] * 12)
        write_list(self.memory, self.heap, LIST_BLOCK, [("gadget", "epic", "Fully Loaded Mad Hatter Satchel Charge", "@Ugr%DXm/*B2", "corrosive", "")], [grenade_seed])
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.names_after_scan(SEEDS), NAMES_BY_SEED)
        self.assertEqual(harness.reader.names_of_items([grenade_seed]), {})
        harness.wait_for_scan()
        self.assertEqual(harness.reader.scans, 2)
        self.assertIn("the inventory list only", harness.log[-2])
        self.assertEqual(harness.reader.names_of_items([grenade_seed]), {grenade_seed: "Fully Loaded Mad Hatter Satchel Charge"})
        self.assertEqual(harness.reader.card_for(grenade_seed).rarity, "epic")

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

    def test_the_node_on_a_chunk_boundary_is_still_found(self) -> None:
        big_block = 0x2000000
        self.memory.block(big_block, SCAN_CHUNK_BYTES + 0x4000)
        write_cards(self.memory, self.heap, node_at=big_block + SCAN_CHUNK_BYTES - 16, cards_at=big_block + 0x1000)
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.names_after_scan(SEEDS), NAMES_BY_SEED)

    def test_a_new_gun_is_read_from_the_remembered_cards_without_a_scan(self) -> None:
        write_cards(self.memory, self.heap)
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.names_after_scan(SEEDS), NAMES_BY_SEED)
        new_seed = bytes([9] * 12)
        self.memory.write(CARDS_AT + LATEST_LAYOUT.cards.record_bytes, make_card(self.heap, "Plasma Coil", "@Ugw$Yw38oGr", "legendary"))
        slots = [SEEDS[0], new_seed, SEEDS[2], SEEDS[3]]
        self.assertNotIn(new_seed, harness.reader.names_for(slots))
        self.assertEqual(harness.reader.names_for(slots)[new_seed], "Plasma Coil")
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
        write_cards(self.memory, self.heap)
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.names_after_scan(SEEDS), NAMES_BY_SEED)
        harness.reader.forget()
        self.assertEqual(harness.reader.known, 0)
        self.assertEqual(harness.names_after_scan(SEEDS), NAMES_BY_SEED)
        self.assertEqual(harness.reader.scans, 2)

    def test_unreadable_name_text_fails_the_card(self) -> None:
        write_cards(self.memory, self.heap)
        broken = bytearray(make_card(self.heap, "Broken", "@Ug", "legendary"))
        broken[LATEST_LAYOUT.cards.name_offset:LATEST_LAYOUT.cards.name_offset + 16] = struct.pack("<QII", 0x7F0000, 8, 8)
        self.memory.write(CARDS_AT, bytes(broken))
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.names_after_scan(SEEDS), {})

    def test_the_scan_reads_the_default_band_first_and_the_last_find_after_that(self) -> None:
        band_low, band_high = itemnames.DEFAULT_SCAN_BAND
        regions = [(0x400000, 0x1000), (band_low + 0x1000, 0x1000), (band_high + 0x1000, 0x1000), (band_low - 0x800, 0x1000)]
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.reader._scan_order(regions), [(band_low + 0x1000, 0x1000), (band_low - 0x800, 0x1000), (0x400000, 0x1000), (band_high + 0x1000, 0x1000)])
        harness.reader._scan_hint = 0x400000
        self.assertEqual(harness.reader._scan_order(regions)[0], (0x400000, 0x1000))

    def test_seeds_of_the_wrong_length_are_ignored(self) -> None:
        harness = ReaderHarness(self.memory)
        self.assertEqual(harness.reader.names_for([b"short"]), {})
        self.assertIsNone(harness.reader._scan)


class SearchNeedleTest(unittest.TestCase):
    def test_trailing_zeros_are_stripped(self) -> None:
        self.assertEqual(itemnames._search_needle(b"\x01\x00\x00\x00\x01\x00\x00\x00"), b"\x01\x00\x00\x00\x01")
        self.assertEqual(itemnames._search_needle(b"\x00\x00"), b"\x00\x00")


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
