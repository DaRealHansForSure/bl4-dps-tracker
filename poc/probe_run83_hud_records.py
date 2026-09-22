"""The HUD's item records (probe 81: name +32, kind +88, rarity +136, maker +168, element
+248, icon path +320, serial +344; the grenade's at 0x2e7e43f88 held by 0x3419781a0, the
gun in hand's at 0x2e7e458a8 held by 0x32e16c7a0, all before any menu). Three questions:
what surrounds the two holders (a node shape to search for?); the full icon path of the
grenade; and whether the icon path's text is a usable locator: every copy of it in the
heaps, every holder of those copies, and which of the holders are records of this shape."""
import ctypes, ctypes.wintypes as wintypes, sys, time, threading
from pathlib import Path
from unrealsdk import logging
import dmgtracker.itemnames as itemnames
OUT = Path(__file__).parent / "probe_output.txt"
def say(m):
    logging.info("[hud83] " + m)
    with OUT.open("a", encoding="utf-8") as f: f.write(m + "\n")
class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p), ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD), ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD), ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]
k = ctypes.WinDLL("kernel32"); k.VirtualQuery.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t); k.VirtualQuery.restype = ctypes.c_size_t
def region_text(a):
    info = MBI(); k.VirtualQuery(ctypes.c_void_p(a), ctypes.byref(info), ctypes.sizeof(info)); return f"region {hex(info.BaseAddress or 0)}+{info.RegionSize >> 10}K"
RECORDS = {"Grenade": (0x2e7e43f88, 0x3419781a0), "gun in hand": (0x2e7e458a8, 0x32e16c7a0)}
NAME, KIND, RARITY, MAKER, ELEMENT, ICON, SERIAL = 32, 88, 136, 168, 248, 320, 344
ICON_NEEDLE = "ico_ui_art_item_card_type/ico_art_item_card_gre"
mod = sys.modules["dmgtracker.mod"]
reader = itemnames.ItemNameReader()
CHUNK, OVERLAP = itemnames.SCAN_CHUNK_BYTES, itemnames.SCAN_OVERLAP_BYTES
say(f"{time.strftime('%H:%M:%S')} HUD records; menu seen {mod._menu_seen}")
def hud_record_at(address):
    """name, kind, rarity, maker, element, serial when a record of the HUD shape is there."""
    rec = reader._read_exact(address, 400)
    if rec is None:
        return None
    name = reader._fstring(rec, NAME)
    kind = reader._fstring(rec, KIND)
    rarity = reader._fstring(rec, RARITY)
    serial = reader._fstring(rec, SERIAL)
    if not name or not kind or not rarity or not serial or not serial.startswith("@U"):
        return None
    return (name.strip(), kind, rarity, reader._fstring(rec, MAKER), reader._fstring(rec, ELEMENT), serial)
def annotate(address, before, after):
    """Every 8-byte word around address: FString text, pointer into the heaps, or the raw value."""
    raw = reader._read(address - before, before + after)
    if raw is None:
        return "unreadable"
    words = []
    for offset in range(0, len(raw) - 8, 8):
        value = int.from_bytes(raw[offset:offset + 8], "little")
        text = reader._fstring(raw, offset, itemnames.MAX_PATH_CHARS) if offset + 16 <= len(raw) else None
        if text:
            words.append((offset - before, f"FString {text[:60]!r}"))
        elif 0x10000 < value < itemnames.POINTER_MAX:
            record = hud_record_at(value)
            words.append((offset - before, f"-> {value:#x}" + (f" HUD RECORD {record[0]!r}" if record else "")))
        else:
            words.append((offset - before, f"{value:#x}"))
    return words
def scan(needles, limit_s, aligned_pointer=False):
    t0 = time.perf_counter(); hits = []; read = 0; where = "band"
    for region_base, size in reader._scan_order(list(reader._regions())):
        for cs in range(region_base, region_base + size, CHUNK):
            chunk = reader._read(cs, min(CHUNK + OVERLAP, region_base + size - cs))
            if chunk is None:
                continue
            read += len(chunk)
            for key, needle in needles.items():
                p = chunk.find(needle)
                while 0 <= p < CHUNK:
                    if not aligned_pointer or (p % 8 == 0 and chunk[p + len(needle):p + 8] == b"\0" * (8 - len(needle))):
                        hits.append((key, cs + p, time.perf_counter() - t0))
                    p = chunk.find(needle, p + 1)
        if time.perf_counter() - t0 > limit_s:
            say(f"   scan stopped after {limit_s} s")
            break
    return hits, time.perf_counter() - t0, read
def run():
    for label, (record, holder) in RECORDS.items():
        found = hud_record_at(record)
        say(f"   {label}: record {record:#x} now {found}")
        rec = reader._read_exact(record, 400)
        if rec:
            say(f"      icon path: {reader._fstring(rec, ICON, 400)!r}")
            say(f"      words 0..32 and 56..88 and 96..136: {annotate(record, 0, 136)}")
        say(f"      holder {holder:#x} ({region_text(holder)}) surroundings: {annotate(holder, 96, 96)}")
    needle = ICON_NEEDLE.encode("utf-16-le")
    hits, seconds, read = scan({"icon": needle}, 60)
    say(f"   pass 1: {len(hits)} copy(ies) of the grenade icon path text in {seconds:.1f} s over {read >> 20} MB")
    strings = {}
    for _key, hit, when in sorted(hits, key=lambda h: h[1]):
        head = reader._read(hit - 2 * len("Texture2D'/Game/uiresources/_shared/assets/"), 2 * 40)
        start = hit - 2 * len("Texture2D'/Game/uiresources/_shared/assets/")
        say(f"      {hit:#x} ({region_text(hit)}) at {when:.1f} s; string start {start:#x}: {head.decode('utf-16-le', 'replace') if head else None!r}")
        strings[start] = hit
    needles = {start: start.to_bytes(8, "little").rstrip(b"\0") for start in strings}
    hits, seconds, read = scan(needles, 120, aligned_pointer=True)
    say(f"   pass 2: {len(hits)} holder(s) of {len(strings)} string(s) in {seconds:.1f} s")
    for start, holder, when in sorted(hits, key=lambda h: h[1]):
        record = hud_record_at(holder - ICON)
        list_record = reader._list_record_at(holder - reader.layout.inventory.thumbnail_offset)
        if record:
            kind = f"HUD RECORD {record}"
        elif list_record:
            kind = f"LIST RECORD {list_record[1].name!r}"
        else:
            kind = "other"
        say(f"      string {start:#x} <- {holder:#x} ({region_text(holder)}) at {when:.1f} s: {kind}")
    say("done")
threading.Thread(target=run, name="hud83", daemon=True).start()
