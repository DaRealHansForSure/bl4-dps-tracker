"""Who holds the name strings Cheat Engine found: pointer scan for the 7 buffer addresses (stripped needles), each holder tried as a card (name at +32, fields dumped when it does not validate) and as a list record (name at +80)."""
import ctypes, ctypes.wintypes as wintypes, time, threading
from pathlib import Path
from unrealsdk import logging
import dmgtracker.itemnames as itemnames
OUT = Path(__file__).parent / "probe_output.txt"
def say(m):
    logging.info("[holders] " + m)
    with OUT.open("a", encoding="utf-8") as f: f.write(m + "\n")
class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p), ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD), ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD), ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]
k = ctypes.WinDLL("kernel32"); k.VirtualQuery.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t); k.VirtualQuery.restype = ctypes.c_size_t
def region_text(a):
    info = MBI(); k.VirtualQuery(ctypes.c_void_p(a), ctypes.byref(info), ctypes.sizeof(info)); return f"region {hex(info.BaseAddress or 0)}+{info.RegionSize >> 10}K alloc {hex(info.AllocationBase or 0)}"
STRINGS = (0x2ED4203D0, 0x3173230D0, 0x327A377C0, 0x136CC83A80, 0x1385F2A7E0, 0x13B1B77AE0, 0x13EE3AF8D0)
reader = itemnames.ItemNameReader()
CHUNK, OVERLAP = itemnames.SCAN_CHUNK_BYTES, itemnames.SCAN_OVERLAP_BYTES
say(f"{time.strftime('%H:%M:%S')} holders of the name strings")
def run():
    L = reader.layout
    for a in STRINGS:
        raw = reader._read(a, 80)
        say(f"   string {a:#x} ({region_text(a)}): {raw[:70].decode('utf-16-le', 'replace') if raw else None!r}")
    needles = {a.to_bytes(8, 'little').rstrip(b'\0'): a for a in STRINGS}
    regs = reader._scan_order(list(reader._regions()))
    t0 = time.perf_counter(); holders = []
    for base, size in regs:
        for cs in range(base, base + size, CHUNK):
            chunk = reader._read(cs, min(CHUNK + OVERLAP, base + size - cs))
            if chunk is None: continue
            for needle, a in needles.items():
                p = chunk.find(needle)
                while 0 <= p < CHUNK:
                    if p % 8 == 0 and chunk[p + len(needle):p + 8] == b"\0" * (8 - len(needle)): holders.append((a, cs + p))
                    p = chunk.find(needle, p + 1)
    say(f"   pointer scan {time.perf_counter() - t0:.1f} s: {len(holders)} holder(s)")
    for a, holder in sorted(holders, key=lambda h: h[1]):
        card = reader._card_at(holder - L.cards.name_offset)
        rec = reader._list_record_at(holder - L.inventory.name_offset)
        if card:
            start = holder - L.cards.name_offset
            while reader._card_at(start - L.cards.record_bytes): start -= L.cards.record_bytes
            cards = reader._cards_at(start)
            say(f"   {a:#x} <- {holder:#x} ({region_text(holder)}): CARD; array {start:#x} x{len(cards)}: {[(c.rarity, c.name) for c in cards]}")
        elif rec:
            say(f"   {a:#x} <- {holder:#x} ({region_text(holder)}): list record '{rec[1].name}'")
        else:
            rec_bytes = reader._read_exact(holder - L.cards.name_offset, L.cards.record_bytes)
            fields = {name: reader._fstring(rec_bytes, off) for name, off in (("slot", L.cards.slot_offset), ("name", L.cards.name_offset), ("serial", L.cards.serial_offset), ("rarity", L.cards.rarity_offset))} if rec_bytes else None
            say(f"   {a:#x} <- {holder:#x} ({region_text(holder)}): other; as card: {fields}")
    say("done")
threading.Thread(target=run, name="holders", daemon=True).start()
