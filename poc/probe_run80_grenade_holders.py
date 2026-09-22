"""Where the equipped grenade's name lives, next to where a gun's lives: every copy of both
name strings in the heaps (pass 1), then every structure holding a pointer to one of them
(pass 2, as probe 66 did): an inventory list record, an item card, or something else,
dumped. Run twice: now, and right after a restart before any menu, to see which holders
of the grenade's name exist before the inventory screen built its list."""
import ctypes, ctypes.wintypes as wintypes, sys, time, threading
from pathlib import Path
from unrealsdk import logging
from mods_base import get_pc
import dmgtracker.itemnames as itemnames
from dmgtracker.itemidentity import seed_of
OUT = Path(__file__).parent / "probe_output.txt"
def say(m):
    logging.info("[holders80] " + m)
    with OUT.open("a", encoding="utf-8") as f: f.write(m + "\n")
class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p), ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD), ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD), ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]
k = ctypes.WinDLL("kernel32"); k.VirtualQuery.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t); k.VirtualQuery.restype = ctypes.c_size_t
def region_text(a):
    info = MBI(); k.VirtualQuery(ctypes.c_void_p(a), ctypes.byref(info), ctypes.sizeof(info)); return f"region {hex(info.BaseAddress or 0)}+{info.RegionSize >> 10}K"
mod = sys.modules["dmgtracker.mod"]
pawn = get_pc().Pawn
gadgets = mod.resolver.equipped_gadgets(pawn, mod.identity_reader.item_id)
weapons = mod.resolver.equipped_weapons(pawn) or []
targets = []
for item_id, label in gadgets.items():
    if label == "Grenade":
        targets.append((label, item_id, mod.name_store.name_for(item_id)))
if weapons:
    targets.append((weapons[0].label, weapons[0].item_id, mod.name_store.name_for(weapons[0].item_id)))
say(f"{time.strftime('%H:%M:%S')} holders of the names; menu seen this session: {mod._menu_seen}; reader known {mod.name_reader.known}, list {hex(mod.name_reader._list_address or 0)}, cards {hex(mod.name_reader._cards_header or 0)}")
for label, item_id, name in targets:
    say(f"   {label} {item_id[:6]}: {name!r}")
reader = itemnames.ItemNameReader()
CHUNK, OVERLAP = itemnames.SCAN_CHUNK_BYTES, itemnames.SCAN_OVERLAP_BYTES
def scan(needles, limit_s):
    """(needle key, address) for every aligned-or-not hit of each needle, band first."""
    t0 = time.perf_counter(); hits = []; read = 0
    for base, size in reader._scan_order(list(reader._regions())):
        for cs in range(base, base + size, CHUNK):
            chunk = reader._read(cs, min(CHUNK + OVERLAP, base + size - cs))
            if chunk is None:
                continue
            read += len(chunk)
            for key, needle in needles.items():
                p = chunk.find(needle)
                while 0 <= p < CHUNK:
                    hits.append((key, cs + p))
                    p = chunk.find(needle, p + 1)
        if time.perf_counter() - t0 > limit_s:
            say(f"   scan stopped after {limit_s} s")
            break
    return hits, time.perf_counter() - t0, read
def as_card_fields(address):
    L = reader.layout.cards
    rec = reader._read_exact(address, L.record_bytes)
    if rec is None:
        return None
    return {n: reader._fstring(rec, o) for n, o in (("slot", L.slot_offset), ("name", L.name_offset), ("elem", L.element_offset), ("dmg", L.damage_offset), ("serial", L.serial_offset), ("rarity", L.rarity_offset))}
def classify(holder, seed):
    L = reader.layout
    rec = reader._list_record_at(holder - L.inventory.name_offset)
    if rec is not None:
        return f"LIST RECORD '{rec[1].name}' (seed matches {rec[0] == seed})"
    card = reader._card_at(holder - L.cards.name_offset)
    if card is not None:
        return f"CARD slot={card.slot_type!r} rarity={card.rarity!r} elem={card.element!r}"
    near = reader._read(holder - 2048, 4096)
    seed_near = near.find(seed) if (near and seed) else -1
    where = f"seed at {seed_near - 2048:+d}" if seed_near >= 0 else "no seed within 2 KB"
    raw = reader._read(holder - 64, 160)
    return f"OTHER ({where}); as card at -32: {as_card_fields(holder - L.cards.name_offset)}; bytes -64..+96: {raw.hex() if raw else None}"
def run():
    needles = {}
    for label, item_id, name in targets:
        if name:
            needles[(label, item_id, "utf16")] = name.encode("utf-16-le").rstrip(b"\0")
            needles[(label, item_id, "utf8")] = name.encode("utf-8")
    hits, seconds, read = scan(needles, 60)
    say(f"   pass 1 (the strings) {seconds:.1f} s over {read >> 20} MB: {len(hits)} copy(ies)")
    strings = {}
    for (label, item_id, encoding), hit in sorted(hits, key=lambda h: h[1]):
        say(f"      {label} {item_id[:6]} {encoding} at {hit:#x} ({region_text(hit)})")
        if encoding == "utf16" and hit % 2 == 0:
            strings[hit] = (label, item_id)
    pointer_needles = {a: a.to_bytes(8, "little").rstrip(b"\0") for a in strings}
    hits, seconds, read = scan(pointer_needles, 120)
    holders = []
    for a, h in hits:
        raw = reader._read_exact(h, 8) if h % 8 == 0 else None
        if raw is not None and int.from_bytes(raw, "little") == a:
            holders.append((a, h))
    say(f"   pass 2 (the holders) {seconds:.1f} s: {len(holders)} aligned holder(s) of {len(strings)} string(s)")
    for a, holder in sorted(holders, key=lambda h: h[1]):
        label, item_id = strings[a]
        say(f"      {label} {item_id[:6]} string {a:#x} <- {holder:#x} ({region_text(holder)}): {classify(holder, seed_of(item_id) or b'')}")
    say("done")
threading.Thread(target=run, name="holders80", daemon=True).start()
