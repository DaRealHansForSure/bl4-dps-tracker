"""Confirm the 1072-byte inventory record layout by finding every equipped gun's seed with a full, fast scan (exact copies, 1 MB chunks), validating the record around each hit and walking the array."""
import ctypes, ctypes.wintypes as wintypes, threading, time
from pathlib import Path
from unrealsdk import logging
from mods_base import get_pc
OUT = Path(__file__).parent / "probe_output.txt"
def say(m):
    logging.info("[listbyseed] " + m)
    with OUT.open("a", encoding="utf-8") as f: f.write(m + "\n")
class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p), ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD), ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD), ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]
k = ctypes.WinDLL("kernel32")
k.VirtualQuery.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t); k.VirtualQuery.restype = ctypes.c_size_t
k.GetCurrentProcess.restype = ctypes.c_void_p
k.ReadProcessMemory.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)); k.ReadProcessMemory.restype = wintypes.BOOL
P = k.GetCurrentProcess()
CHUNK = 1 << 20; OVERLAP = 16
RECORD = 1072; SEED_OFF = 632
FIELDS = {0: "slot", 24: "type", 48: "type_text", 80: "name", 104: "type_text2", 136: "slot2", 160: "rarity_text", 184: "rarity", 216: "maker_text", 240: "description", 264: "maker", 296: "element", 320: "element2", 368: "thumbnail", 392: "serial"}
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
def fstring(rec, off):
    p = int.from_bytes(rec[off:off+8], "little"); num = int.from_bytes(rec[off+8:off+12], "little", signed=True); cap = int.from_bytes(rec[off+12:off+16], "little", signed=True)
    if num == 0 and cap >= 0: return ""
    if not (0x10000 <= p <= 0x7FFFFFFFFFFF) or num < 1 or num > 400 or cap < num: return None
    raw = read(p, num * 2)
    if raw is None or raw[-2:] != b"\0\0": return None
    try: t = raw[:-2].decode("utf-16-le")
    except UnicodeDecodeError: return None
    if any(ord(c) < 32 for c in t): return None
    return t
def record_at(a):
    rec = read(a, RECORD)
    if rec is None: return None
    slot = fstring(rec, 0); name = fstring(rec, 80); rarity = fstring(rec, 184); serial = fstring(rec, 392)
    if not slot or not name or not rarity or not serial or not serial.startswith("@U") or not slot.isalpha(): return None
    return {"slot": slot, "type": fstring(rec, 24), "name": name, "rarity": rarity, "maker": fstring(rec, 264), "element": fstring(rec, 296), "serial": serial[:12], "seed": rec[SEED_OFF:SEED_OFF+12].hex(), "raw": rec}
def run(seeds):
    needles = list(seeds.values())
    t0 = time.perf_counter()
    regs = list(regions())
    t1 = time.perf_counter()
    buf = ctypes.create_string_buffer(CHUNK + OVERLAP); got = ctypes.c_size_t(0)
    hits = []; scanned = 0; since_sleep = 0; read_s = 0.0; find_s = 0.0
    for base, size in regs:
        cs = base
        while cs < base + size:
            n = min(CHUNK + OVERLAP, base + size - cs)
            ta = time.perf_counter()
            k.ReadProcessMemory(P, ctypes.c_void_p(cs), buf, n, ctypes.byref(got))
            if got.value == 0:
                cs += CHUNK; continue
            data = ctypes.string_at(buf, got.value)
            tb = time.perf_counter()
            for label, needle in seeds.items():
                p = data.find(needle)
                while p >= 0:
                    hits.append((label, cs + p)); p = data.find(needle, p + 1)
            tc = time.perf_counter()
            read_s += tb - ta; find_s += tc - tb; scanned += got.value; since_sleep += got.value
            cs += CHUNK
            if since_sleep >= 8 << 20:
                since_sleep = 0; time.sleep(0.001)
    t2 = time.perf_counter()
    hits = sorted(set(hits), key=lambda h: h[1])
    say(f"   {len(regs)} regions in {t1 - t0:.2f} s; scanned {scanned / 1e9:.2f} GB in {t2 - t1:.1f} s (reads {read_s:.1f} s, finds {find_s:.1f} s); {len(hits)} hits")
    lists = {}
    for label, hit in hits:
        info = query(hit)
        region = f"region {hex(info.BaseAddress or 0)} +{info.RegionSize >> 10} KB alloc {hex(info.AllocationBase or 0)}" if info else "?"
        rec = record_at(hit - SEED_OFF)
        say(f"   {label} at {hit:#x}: {region}: " + (f"record '{rec['name']}' {rec['slot']}/{rec['type']}/{rec['rarity']}/{rec['maker']}/{rec['element']} serial {rec['serial']}.. seed {rec['seed'][:8]}" if rec else "no record"))
        if rec:
            start = hit - SEED_OFF
            while record_at(start - RECORD) is not None: start -= RECORD
            count = 0
            while count < 200 and record_at(start + count * RECORD) is not None: count += 1
            lists[start] = count
    for start, count in lists.items():
        say(f"   list {start:#x} x{count}:")
        for i in range(count):
            r = record_at(start + i * RECORD)
            say(f"      {i:2d} {r['slot']:8s} {r['type'] or '':14s} {r['rarity']:10s} {r['seed'][:8]} {r['serial']} {r['name']}")
        raw = record_at(start)["raw"]
        for off in range(0, RECORD - 16, 8):
            t = fstring(raw, off)
            if t: say(f"      +{off}: {FIELDS.get(off, '?')} = {t[:80]!r}")
        words = [hex(int.from_bytes(raw[o:o+8], "little")) for o in range(416, 640, 8)]
        say(f"      words 416..640: {words}")
        words = [hex(int.from_bytes(raw[o:o+8], "little")) for o in range(640, 672, 8)]
        say(f"      words 640..672: {words}")
    say("done")
say(f"{time.strftime('%H:%M:%S')} inventory list by seed")
try:
    seeds = {}
    for i, slot in enumerate(get_pc().Pawn.ActiveWeapons.Slots):
        w = slot.Weapon
        if w is None: continue
        identity = int(w.Item.data.Identity._get_address())
        seeds[f"slot{i} {w.BodyData.Name}"] = read(identity + 200, 12)
    say(f"   seeds: {[(l, s.hex()[:8]) for l, s in seeds.items()]}")
    threading.Thread(target=run, args=(seeds,), name="listbyseed", daemon=True).start()
except Exception as ex:
    import traceback; say(f"   failed: {ex}\n{traceback.format_exc()[-1200:]}")
