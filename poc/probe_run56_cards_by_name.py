"""Locate every structure holding the name of the gun in hand: name string buffers, then the 8-byte pointers to them, each tried as the name field of a 280-byte card (+32) or a 1072-byte list record (+80). Also whether 'Lionized Parasite' is in any of them."""
import ctypes, ctypes.wintypes as wintypes, threading, time
from pathlib import Path
from unrealsdk import logging
OUT = Path(__file__).parent / "probe_output.txt"
def say(m):
    logging.info("[cardsbyname] " + m)
    with OUT.open("a", encoding="utf-8") as f: f.write(m + "\n")
class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p), ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD), ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD), ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]
k = ctypes.WinDLL("kernel32")
k.VirtualQuery.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t); k.VirtualQuery.restype = ctypes.c_size_t
k.GetCurrentProcess.restype = ctypes.c_void_p
k.ReadProcessMemory.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)); k.ReadProcessMemory.restype = wintypes.BOOL
P = k.GetCurrentProcess()
CHUNK = 1 << 20; OVERLAP = 128
NAME = "Maintained Screeching T.K's Wave".encode("utf-16-le")
OTHER = "Lionized Parasite".encode("utf-16-le")
def query(a):
    info = MBI()
    if k.VirtualQuery(ctypes.c_void_p(a), ctypes.byref(info), ctypes.sizeof(info)) == 0: return None
    return info
def regions():
    a = 0
    while a < 0x7FFFFFFFFFFF:
        info = query(a)
        if info is None: break
        base = info.BaseAddress or 0; size = info.RegionSize
        if info.State == 0x1000 and info.Type == 0x20000 and info.Protect == 0x04:
            yield base, size
        a = base + size
def read(a, n):
    if a < 0x10000 or n <= 0: return None
    buf = ctypes.create_string_buffer(n); got = ctypes.c_size_t(0)
    k.ReadProcessMemory(P, ctypes.c_void_p(a), buf, n, ctypes.byref(got))
    return buf.raw if got.value == n else None
def scan(needles, regs):
    buf = ctypes.create_string_buffer(CHUNK + OVERLAP); got = ctypes.c_size_t(0)
    hits = {n: [] for n in needles}; since = 0
    for base, size in regs:
        cs = base
        while cs < base + size:
            n = min(CHUNK + OVERLAP, base + size - cs)
            k.ReadProcessMemory(P, ctypes.c_void_p(cs), buf, n, ctypes.byref(got))
            if got.value:
                data = ctypes.string_at(buf, got.value)
                for needle in needles:
                    p = data.find(needle)
                    while p >= 0:
                        if p < CHUNK: hits[needle].append(cs + p)
                        p = data.find(needle, p + 1)
                since += got.value
            cs += CHUNK
            if since >= 8 << 20:
                since = 0; time.sleep(0.001)
    return hits
def fstring(rec, off, limit=400):
    p = int.from_bytes(rec[off:off+8], "little"); num = int.from_bytes(rec[off+8:off+12], "little", signed=True); cap = int.from_bytes(rec[off+12:off+16], "little", signed=True)
    if num == 0 and cap >= 0: return ""
    if not (0x10000 <= p <= 0x7FFFFFFFFFFF) or num < 1 or num > limit or cap < num: return None
    raw = read(p, num * 2)
    if raw is None or raw[-2:] != b"\0\0": return None
    try: t = raw[:-2].decode("utf-16-le")
    except UnicodeDecodeError: return None
    if any(ord(c) < 32 for c in t): return None
    return t
def card_at(a):
    rec = read(a, 280)
    if rec is None: return None
    slot = fstring(rec, 8); name = fstring(rec, 32); serial = fstring(rec, 224); rarity = fstring(rec, 248)
    if not slot or not name or not serial or not rarity or not serial.startswith("@U") or not slot.isalpha(): return None
    return {"slot": slot, "name": name, "serial": serial[:12], "rarity": rarity, "element": fstring(rec, 136), "damage": fstring(rec, 168)}
def list_record_at(a):
    rec = read(a, 1072)
    if rec is None: return None
    slot = fstring(rec, 0); name = fstring(rec, 80); serial = fstring(rec, 392); rarity = fstring(rec, 184)
    if not slot or not name or not serial or not rarity or not serial.startswith("@U") or not slot.isalpha(): return None
    return {"slot": slot, "name": name, "serial": serial[:12], "rarity": rarity, "seed": rec[632:644].hex()[:8]}
def walk(start, stride, probe):
    while probe(start - stride) is not None: start -= stride
    count = 0
    while count < 300 and probe(start + count * stride) is not None: count += 1
    return start, count
def region_text(a):
    info = query(a)
    return f"region {hex(info.BaseAddress or 0)}+{info.RegionSize >> 10}K alloc {hex(info.AllocationBase or 0)}" if info else "?"
def run():
    t0 = time.perf_counter()
    regs = list(regions())
    strings = scan([NAME, OTHER], regs)
    t1 = time.perf_counter()
    say(f"   pass A {t1 - t0:.1f} s: name buffers {[hex(a) for a in strings[NAME]]}; 'Lionized Parasite' buffers {[hex(a) for a in strings[OTHER]]}")
    needles = [a.to_bytes(8, "little") for a in strings[NAME]]
    holders = scan(needles, regs)
    t2 = time.perf_counter()
    say(f"   pass B {t2 - t1:.1f} s: {sum(len(v) for v in holders.values())} holder(s)")
    cards = {}; lists = {}
    for needle, addresses in holders.items():
        for holder in addresses:
            card = card_at(holder - 32); rec = list_record_at(holder - 80)
            kind = "card" if card else ("list record" if rec else "other")
            say(f"   string {hex(int.from_bytes(needle, 'little'))} <- holder {holder:#x} ({region_text(holder)}): {kind}")
            if card:
                start, count = walk(holder - 32, 280, card_at); cards[start] = count
            if rec:
                start, count = walk(holder - 80, 1072, list_record_at); lists[start] = count
    for start, count in cards.items():
        say(f"   cards {start:#x} x{count} ({region_text(start)}):")
        for i in range(count):
            c = card_at(start + i * 280)
            say(f"      {i} {c['slot']:8s} {c['rarity']:10s} {c['element'] or '':10s} {c['damage'] or '':12s} {c['serial']} {c['name']!r}")
    for start, count in lists.items():
        names = [list_record_at(start + i * 1072)["name"] for i in range(count)]
        say(f"   list {start:#x} x{count} ({region_text(start)}): {names}")
    say("done")
say(f"{time.strftime('%H:%M:%S')} structures holding the name of the gun in hand")
threading.Thread(target=run, name="cardsbyname", daemon=True).start()
