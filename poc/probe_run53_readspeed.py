"""Where the read time goes: ReadProcessMemory versus memmove on the same resident region, at several chunk sizes, plus the cost of the largest regions."""
import ctypes, ctypes.wintypes as wintypes, threading, time
from pathlib import Path
from unrealsdk import logging
OUT = Path(__file__).parent / "probe_output.txt"
def say(m):
    logging.info("[readspeed] " + m)
    with OUT.open("a", encoding="utf-8") as f: f.write(m + "\n")
class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p), ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD), ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD), ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]
k = ctypes.WinDLL("kernel32")
k.VirtualQuery.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t); k.VirtualQuery.restype = ctypes.c_size_t
k.GetCurrentProcess.restype = ctypes.c_void_p
k.ReadProcessMemory.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)); k.ReadProcessMemory.restype = wintypes.BOOL
P = k.GetCurrentProcess()
def regions():
    a = 0
    while a < 0x7FFFFFFFFFFF:
        info = MBI()
        if k.VirtualQuery(ctypes.c_void_p(a), ctypes.byref(info), ctypes.sizeof(info)) == 0: break
        base = info.BaseAddress or 0; size = info.RegionSize
        if info.State == 0x1000 and info.Type == 0x20000 and info.Protect == 0x04:
            yield base, size
        a = base + size
def run():
    regs = list(regions())
    regs.sort(key=lambda r: -r[1])
    say(f"   {len(regs)} regions; largest {[(hex(b), s >> 20) for b, s in regs[:6]]} MB")
    sizes = sorted(s for _b, s in regs)
    small = sum(1 for s in sizes if s <= 64 * 1024)
    say(f"   regions <= 64 KB: {small}, <= 1 MB: {sum(1 for s in sizes if s <= 1 << 20)}, > 16 MB: {sum(1 for s in sizes if s > 16 << 20)}; bytes in regions > 16 MB: {sum(s for s in sizes if s > 16 << 20) / 1e9:.2f} GB")
    base, size = regs[0]
    got = ctypes.c_size_t(0)
    for chunk in (64 * 1024, 1 << 20, 8 << 20, 32 << 20):
        buf = ctypes.create_string_buffer(chunk)
        total = min(size, 256 << 20)
        t0 = time.perf_counter()
        for cs in range(base, base + total, chunk):
            k.ReadProcessMemory(P, ctypes.c_void_p(cs), buf, min(chunk, base + total - cs), ctypes.byref(got))
        t1 = time.perf_counter()
        for cs in range(base, base + total, chunk):
            ctypes.memmove(buf, ctypes.c_void_p(cs), min(chunk, base + total - cs))
        t2 = time.perf_counter()
        for cs in range(base, base + total, chunk):
            data = buf.raw
        t3 = time.perf_counter()
        say(f"   chunk {chunk >> 10} KB over {total >> 20} MB: ReadProcessMemory {total / 1e6 / (t1 - t0):.0f} MB/s, memmove {total / 1e6 / (t2 - t1):.0f} MB/s, buf.raw {total / 1e6 / (t3 - t2):.0f} MB/s")
    # per-call overhead on tiny reads
    buf = ctypes.create_string_buffer(4096)
    t0 = time.perf_counter()
    for i in range(20000):
        k.ReadProcessMemory(P, ctypes.c_void_p(base + (i % 1000) * 4096), buf, 4096, ctypes.byref(got))
    t1 = time.perf_counter()
    say(f"   20000 x 4 KB ReadProcessMemory: {(t1 - t0) * 1e6 / 20000:.1f} us per call")
    say("done")
threading.Thread(target=run, name="readspeed", daemon=True).start()
say(f"{time.strftime('%H:%M:%S')} read speed test started")
