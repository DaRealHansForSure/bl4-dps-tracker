"""Find the item cards' TArray header by content in one pass: the {num, max} pair (N, N..N+4) as a stripped needle, the word before it a pointer to a record that validates as a card. Counts candidates and times the pass."""
import sys, time, threading
from pathlib import Path
from unrealsdk import logging
from mods_base import get_pc
import dmgtracker.itemnames as itemnames
OUT = Path(__file__).parent / "probe_output.txt"
def say(m):
    logging.info("[headerscan] " + m)
    with OUT.open("a", encoding="utf-8") as f: f.write(m + "\n")
mod = sys.modules["dmgtracker.mod"]
equipped = mod.resolver.equipped_weapons(get_pc().Pawn) or []
N = len(equipped)
say(f"{time.strftime('%H:%M:%S')} header scan for {N} card(s)")
reader = itemnames.ItemNameReader()
CHUNK, OVERLAP = itemnames.SCAN_CHUNK_BYTES, itemnames.SCAN_OVERLAP_BYTES
def run():
    L = reader.layout
    regs = reader._scan_order(list(reader._regions()))
    needles = [(N.to_bytes(4, "little") + cap.to_bytes(4, "little")).rstrip(b"\0") for cap in range(N, N + 5)]
    t0 = time.perf_counter(); matches = 0; plausible = 0; prefiltered = 0; found = {}
    for base, size in regs:
        for cs in range(base, base + size, CHUNK):
            chunk = reader._read(cs, min(CHUNK + OVERLAP, base + size - cs))
            if chunk is None: continue
            for needle in needles:
                p = chunk.find(needle)
                while 0 <= p < CHUNK:
                    if p % 8 == 0 and p >= 8 and chunk[p + len(needle):p + 8] == b"\0" * (8 - len(needle)):
                        matches += 1
                        data = int.from_bytes(chunk[p - 8:p], "little")
                        if itemnames._is_pointer(data):
                            plausible += 1
                            record = reader._read_exact(data, L.cards.record_bytes)
                            if record is not None and reader._card_prefilter(record, 0):
                                prefiltered += 1
                                cards = reader._cards_at(data)
                                if len(cards) == N:
                                    found[cs + p - 8] = (data, [(c.rarity, c.name) for c in cards])
                    p = chunk.find(needle, p + 1)
            if found and (time.perf_counter() - t0) > 0:
                pass
    say(f"   {time.perf_counter() - t0:.1f} s: {matches} matches, {plausible} plausible pointers, {prefiltered} prefiltered records; headers {[(hex(h), hex(d), cards) for h, (d, cards) in found.items()]}")
    say("done")
threading.Thread(target=run, name="headerscan", daemon=True).start()
