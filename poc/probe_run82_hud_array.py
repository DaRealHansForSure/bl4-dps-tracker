"""The array that holds the HUD's item records (probe 81: pointers 24 bytes apart at
0x186b6498 and 0x186b64b0, the grenade and the gun in hand). Who holds the array (its
{data, num, max} header, and the node around it, to compare with the cards' node
signature), how long it is, what the 16 bytes after each record pointer are, and every
record's name, rarity, maker, element and serial."""
import sys, time, threading
from pathlib import Path
from unrealsdk import logging
import dmgtracker.itemnames as itemnames
OUT = Path(__file__).parent / "probe_output.txt"
def say(m):
    logging.info("[hudarray82] " + m)
    with OUT.open("a", encoding="utf-8") as f: f.write(m + "\n")
ENTRY_BYTES = 24
FIRST_ENTRY = 0x186b6498
CANDIDATE_STARTS = [FIRST_ENTRY - back * ENTRY_BYTES for back in range(0, 8)]
mod = sys.modules["dmgtracker.mod"]
reader = itemnames.ItemNameReader()
CHUNK, OVERLAP = itemnames.SCAN_CHUNK_BYTES, itemnames.SCAN_OVERLAP_BYTES
say(f"{time.strftime('%H:%M:%S')} HUD array; menu seen {mod._menu_seen}")
def record_text(address):
    rec = reader._read_exact(address, 400)
    if rec is None:
        return None
    name = reader._fstring(rec, 32)
    rarity = reader._fstring(rec, 136)
    if not name or rarity is None:
        return None
    return f"{name.strip()!r} kind {reader._fstring(rec, 88)!r} rarity {rarity!r} maker {reader._fstring(rec, 168)!r} elem {reader._fstring(rec, 248)!r} serial {reader._fstring(rec, 344)!r}"
def dump_entries(start, count):
    data = reader._read_exact(start, count * ENTRY_BYTES)
    if data is None:
        say(f"      entries at {start:#x}: unreadable")
        return
    for index in range(count):
        entry = data[index * ENTRY_BYTES:(index + 1) * ENTRY_BYTES]
        pointer = int.from_bytes(entry[:8], "little")
        say(f"      [{index}] {pointer:#x} + {entry[8:].hex()}: {record_text(pointer) if 0x10000 < pointer < itemnames.POINTER_MAX else '-'}")
def run():
    # First the array itself, read from the first entry backwards while the entries look
    # like record pointers, to find its start and length without the header.
    start = FIRST_ENTRY
    while record_text(int.from_bytes(reader._read_exact(start - ENTRY_BYTES, 8) or b"\0" * 8, "little")) is not None:
        start -= ENTRY_BYTES
    count = 0
    while count < 64 and record_text(int.from_bytes(reader._read_exact(start + count * ENTRY_BYTES, 8) or b"\0" * 8, "little")) is not None:
        count += 1
    say(f"   entries by content: start {start:#x}, {count} record pointer(s) in a row")
    dump_entries(start, count)
    # Then who holds the start (the header) and the node around the header.
    needles = {candidate: candidate.to_bytes(8, "little").rstrip(b"\0") for candidate in {start, FIRST_ENTRY}}
    t0 = time.perf_counter(); holders = []
    for region_base, size in reader._scan_order(list(reader._regions())):
        for cs in range(region_base, region_base + size, CHUNK):
            chunk = reader._read(cs, min(CHUNK + OVERLAP, region_base + size - cs))
            if chunk is None:
                continue
            for target, needle in needles.items():
                p = chunk.find(needle)
                while 0 <= p < CHUNK:
                    if p % 8 == 0 and chunk[p + len(needle):p + 8] == b"\0" * (8 - len(needle)):
                        holders.append((target, cs + p))
                    p = chunk.find(needle, p + 1)
        if time.perf_counter() - t0 > 90:
            say("   holder scan stopped after 90 s")
            break
    say(f"   holder scan {time.perf_counter() - t0:.1f} s: {len(holders)} holder(s)")
    for target, holder in sorted(holders, key=lambda h: h[1]):
        window = reader._read(holder - 64, 128)
        if window is None:
            continue
        num = int.from_bytes(window[72:76], "little", signed=True)
        cap = int.from_bytes(window[76:80], "little", signed=True)
        say(f"      {target:#x} <- {holder:#x}: num {num} max {cap}; bytes -64..+64: {window.hex()}")
        cards_node = reader._node_in(window[64 - len(reader.layout.cards_node.before):64 - len(reader.layout.cards_node.before) + reader.layout.cards_node.window_bytes]) if 64 - len(reader.layout.cards_node.before) >= 0 else None
        say(f"         as the cards' node shape: {cards_node}")
    say("done")
threading.Thread(target=run, name="hudarray82", daemon=True).start()
