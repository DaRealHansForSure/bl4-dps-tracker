"""The third model probe 80 turned up before any menu: a record with the item's name at
+32, its rarity at +136, its manufacturer at +168 and its element at +248, one for the
grenade and one for a gun. Dump every FString field of both records, then find who holds
pointers to them (their container) and whether the neighbours of that pointer point at
records of the same shape (an array of the equipped items, in which order)."""
import sys, time, threading
from pathlib import Path
from unrealsdk import logging
from mods_base import get_pc
import dmgtracker.itemnames as itemnames
from dmgtracker.itemidentity import seed_of
OUT = Path(__file__).parent / "probe_output.txt"
def say(m):
    logging.info("[summary81] " + m)
    with OUT.open("a", encoding="utf-8") as f: f.write(m + "\n")
# The name FString holders probe 80 found (this session), minus the name offset.
NAME_OFFSET = 32
RECORDS = {"Grenade": 0x2e7e43fa8 - NAME_OFFSET, "Vladof Assault Rifle": 0x2e7e458c8 - NAME_OFFSET}
BEFORE, AFTER = 256, 1024
mod = sys.modules["dmgtracker.mod"]
reader = itemnames.ItemNameReader()
CHUNK, OVERLAP = itemnames.SCAN_CHUNK_BYTES, itemnames.SCAN_OVERLAP_BYTES
pawn = get_pc().Pawn
gadgets = mod.resolver.equipped_gadgets(pawn, mod.identity_reader.item_id)
weapons = mod.resolver.equipped_weapons(pawn) or []
seeds = {label: seed_of(item_id) for item_id, label in gadgets.items()}
for index, weapon in enumerate(weapons):
    seeds[f"slot {index} {weapon.label}"] = seed_of(weapon.item_id)
say(f"{time.strftime('%H:%M:%S')} summary records; menu seen {mod._menu_seen}; equipped: {[(k, (v or b'').hex()[:6]) for k, v in seeds.items()]}")
def fstrings_in(buffer, base_offset):
    """(offset, text) for every 8-aligned FString header in the buffer."""
    found = []
    for offset in range(0, len(buffer) - 16, 8):
        text = reader._fstring(buffer, offset, itemnames.MAX_PATH_CHARS)
        if text:
            found.append((offset - base_offset, text[:90]))
    return found
def ints_in(buffer, base_offset):
    """(offset, value) for every 4-aligned small int in the buffer, to spot counts, slots, ids."""
    found = []
    for offset in range(0, len(buffer) - 4, 4):
        value = int.from_bytes(buffer[offset:offset + 4], "little", signed=True)
        if 0 < abs(value) < 100000:
            found.append((offset - base_offset, value))
    return found
def record_name_at(address):
    """The name FString of a record of the summary shape at address, or None."""
    rec = reader._read_exact(address, 288)
    if rec is None:
        return None
    name = reader._fstring(rec, NAME_OFFSET)
    rarity = reader._fstring(rec, 136)
    if not name or rarity is None:
        return None
    return f"{name.strip()!r} rarity {rarity!r} maker {reader._fstring(rec, 168)!r} elem {reader._fstring(rec, 248)!r}"
def run():
    for label, base in RECORDS.items():
        buffer = reader._read(base - BEFORE, BEFORE + AFTER)
        if buffer is None:
            say(f"   {label} record {base:#x}: unreadable")
            continue
        say(f"   {label} record {base:#x}: FStrings {fstrings_in(buffer, BEFORE)}")
        seed_hits = [(k, buffer.find(v) - BEFORE) for k, v in seeds.items() if v and buffer.find(v) >= 0]
        say(f"      seeds within -256..+1024: {seed_hits}")
        say(f"      small ints -64..+320: {[(o, v) for o, v in ints_in(buffer, BEFORE) if -64 <= o < 320]}")
    needles = {base: base.to_bytes(8, "little").rstrip(b"\0") for base in RECORDS.values()}
    t0 = time.perf_counter(); holders = []
    for region_base, size in reader._scan_order(list(reader._regions())):
        for cs in range(region_base, region_base + size, CHUNK):
            chunk = reader._read(cs, min(CHUNK + OVERLAP, region_base + size - cs))
            if chunk is None:
                continue
            for base, needle in needles.items():
                p = chunk.find(needle)
                while 0 <= p < CHUNK:
                    if p % 8 == 0 and chunk[p + len(needle):p + 8] == b"\0" * (8 - len(needle)):
                        holders.append((base, cs + p))
                    p = chunk.find(needle, p + 1)
        if time.perf_counter() - t0 > 90:
            say("   holder scan stopped after 90 s")
            break
    say(f"   holder scan {time.perf_counter() - t0:.1f} s: {len(holders)} holder(s)")
    labels = {base: label for label, base in RECORDS.items()}
    for base, holder in sorted(holders, key=lambda h: h[1]):
        around = reader._read(holder - 256, 512)
        if around is None:
            say(f"      {labels[base]} {base:#x} <- {holder:#x}: unreadable around")
            continue
        neighbours = []
        for offset in range(0, 512, 8):
            pointer = int.from_bytes(around[offset:offset + 8], "little")
            if 0x10000 < pointer < itemnames.POINTER_MAX and pointer % 8 == 0:
                name = record_name_at(pointer)
                if name:
                    neighbours.append((offset - 256, f"{pointer:#x}", name))
        say(f"      {labels[base]} {base:#x} <- {holder:#x}: {len(neighbours)} record pointer(s) within -256..+256: {neighbours}")
    say("done")
threading.Thread(target=run, name="summary81", daemon=True).start()
