"""The 288-byte slot records around the seed hit of probe 54, their non-string fields, and the holders of the pointers to the slot records and to the 280-byte cards (their TArray headers)."""
import ctypes, ctypes.wintypes as wintypes, threading, time
from pathlib import Path
from unrealsdk import logging
OUT = Path(__file__).parent / "probe_output.txt"
def say(m):
    logging.info("[slots] " + m)
    with OUT.open("a", encoding="utf-8") as f: f.write(m + "\n")
class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p), ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD), ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD), ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]
k = ctypes.WinDLL("kernel32")
k.VirtualQuery.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t); k.VirtualQuery.restype = ctypes.c_size_t
k.GetCurrentProcess.restype = ctypes.c_void_p
k.ReadProcessMemory.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)); k.ReadProcessMemory.restype = wintypes.BOOL
P = k.GetCurrentProcess()
CHUNK = 1 << 20; OVERLAP = 128
SEED_HIT = 0x329d8930; CARDS = 0x329d6050
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
def slot_at(a):
    rec = read(a, 288)
    if rec is None: return None
    kind = fstring(rec, 0); rarity = fstring(rec, 24)
    if not kind or not rarity or not kind.isalpha() or not rarity.isalpha(): return None
    return {"type": kind, "rarity": rarity, "seed": rec[248:260].hex()[:8], "raw": rec}
def card_at(a):
    rec = read(a, 280)
    if rec is None: return None
    slot = fstring(rec, 8); name = fstring(rec, 32); serial = fstring(rec, 224); rarity = fstring(rec, 248)
    if not slot or not name or not serial or not rarity or not serial.startswith("@U") or not slot.isalpha(): return None
    return {"slot": slot, "name": name, "rarity": rarity, "raw": rec}
def words(raw, lo, hi):
    return [hex(int.from_bytes(raw[o:o+8], "little")) for o in range(lo, hi, 8)]
def run():
    start = SEED_HIT - 248
    while slot_at(start - 288) is not None: start -= 288
    count = 0
    while count < 16 and slot_at(start + count * 288) is not None: count += 1
    say(f"   slot records {start:#x} x{count} (cards at {CARDS:#x}, {CARDS - start:+#x} away)")
    for i in range(count):
        s = slot_at(start + i * 288)
        say(f"      {i} {s['type']:14s} {s['rarity']:10s} seed {s['seed']} words 264..288 {words(s['raw'], 264, 288)} words 48..64 {words(s['raw'], 48, 64)}")
    cstart = CARDS
    ccount = 0
    while ccount < 16 and card_at(cstart + ccount * 280) is not None: ccount += 1
    say(f"   cards {cstart:#x} x{ccount}")
    for i in range(ccount):
        c = card_at(cstart + i * 280)
        say(f"      {i} {c['slot']} {c['rarity']:10s} {c['name']!r} words 0..8 {words(c['raw'], 0, 8)} words 24..32 {words(c['raw'], 24, 32)} words 264..280 {words(c['raw'], 264, 280)}")
    regs = list(regions())
    t0 = time.perf_counter()
    needles = {start.to_bytes(8, "little"): "slots", CARDS.to_bytes(8, "little"): "cards"}
    hits = scan(list(needles), regs)
    say(f"   pointer scan {time.perf_counter() - t0:.1f} s")
    for needle, addresses in hits.items():
        for holder in addresses:
            around = read(holder - 32, 80)
            info = query(holder)
            region = f"region {hex(info.BaseAddress or 0)}+{info.RegionSize >> 10}K alloc {hex(info.AllocationBase or 0)}" if info else "?"
            say(f"   {needles[needle]} <- holder {holder:#x} ({region}): words -32..48 {words(around, 0, 80) if around else None}")
    say("done")
say(f"{time.strftime('%H:%M:%S')} slot records and array headers")
threading.Thread(target=run, name="slots", daemon=True).start()
