"""Which UI models hold the equipped guns right now: a scan for every equipped seed (the reader's own scanner), each hit classified as inventory record, slot record (with the card search result) or other."""
import sys, time, threading
from pathlib import Path
from unrealsdk import logging
from mods_base import get_pc
import dmgtracker.itemnames as itemnames
OUT = Path(__file__).parent / "probe_output.txt"
def say(m):
    logging.info("[modelsnow] " + m)
    with OUT.open("a", encoding="utf-8") as f: f.write(m + "\n")
mod = sys.modules["dmgtracker.mod"]
pawn = get_pc().Pawn
equipped = mod.resolver.equipped_weapons(pawn) or []
seeds = {w.label: bytes.fromhex(w.item_id) for w in equipped if len(w.item_id) == 24}
say(f"{time.strftime('%H:%M:%S')} models now for {list(seeds)}")
reader = itemnames.ItemNameReader()
def classify(hit, label):
    L = reader.layout
    rec = reader._list_record_at(hit - L.inventory.seed_offset)
    if rec is not None:
        start = reader._list_start(hit - L.inventory.seed_offset)
        say(f"   {label} {hit:#x}: inventory record '{rec[1].name}' in list {start:#x} x{len(reader._list_at(start))}")
        return
    slot = reader._slot_at(hit - L.slots.seed_offset)
    if slot is not None:
        start = reader._slots_start(hit - L.slots.seed_offset)
        slots = reader._slots_at(start)
        say(f"   {label} {hit:#x}: slot record; slots {start:#x} x{len(slots)}: {[(s.type_slug, s.rarity, s.seed.hex()[:6]) for s in slots]}")
        arrays = list(reader._card_arrays_in(start - itemnames.CARDS_WINDOW_BYTES, start + itemnames.CARDS_WINDOW_BYTES))
        say(f"      card arrays in the window: {[(hex(a), [(c.rarity, c.name) for c in cards]) for a, cards in arrays]}")
        say(f"      pairing: {reader._cards_near(start, slots)}")
        return
    say(f"   {label} {hit:#x}: other")
def run():
    t0 = time.perf_counter()
    regs = reader._scan_order(list(reader._regions()))
    hits = []
    for base, size in regs:
        for cs in range(base, base + size, itemnames.SCAN_CHUNK_BYTES):
            chunk = reader._read(cs, min(itemnames.SCAN_CHUNK_BYTES + itemnames.SCAN_OVERLAP_BYTES, base + size - cs))
            if chunk is None: continue
            for label, seed in seeds.items():
                p = chunk.find(seed)
                while 0 <= p < itemnames.SCAN_CHUNK_BYTES:
                    hits.append((label, cs + p)); p = chunk.find(seed, p + 1)
    say(f"   {len(hits)} hit(s) in {time.perf_counter() - t0:.1f} s")
    for label, hit in sorted(hits, key=lambda h: h[1]):
        try: classify(hit, label)
        except Exception as ex: say(f"   {label} {hit:#x}: classify failed {ex!r}")
    say("done")
threading.Thread(target=run, name="modelsnow", daemon=True).start()
