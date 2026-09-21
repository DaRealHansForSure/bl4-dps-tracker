"""Walk whole allocations that hold binding-map entries, looking for the pointer to the card array probe 66 found (0x137e2a1be0), and for the thumbnail paths of those cards."""
import ctypes, ctypes.wintypes as wintypes, time, threading
from pathlib import Path
from unrealsdk import logging
import dmgtracker.itemnames as itemnames
OUT = Path(__file__).parent / "probe_output.txt"
def say(m):
    logging.info("[allocwalk] " + m)
    with OUT.open("a", encoding="utf-8") as f: f.write(m + "\n")
class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p), ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD), ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD), ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]
k = ctypes.WinDLL("kernel32"); k.VirtualQuery.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t); k.VirtualQuery.restype = ctypes.c_size_t
CARDS = 0x137e2a1be0
ALLOCS = (0xc040000, 0x55d20000, 0x12bea0000, 0x39b1f0000, 0x1215650000, 0x2ed420000)
reader = itemnames.ItemNameReader()
say(f"{time.strftime('%H:%M:%S')} allocation walk")
def regions_of(alloc):
    a = alloc
    while True:
        info = MBI()
        if k.VirtualQuery(ctypes.c_void_p(a), ctypes.byref(info), ctypes.sizeof(info)) == 0: return
        if (info.AllocationBase or 0) != alloc: return
        if info.State == 0x1000 and info.Protect == 0x04: yield (info.BaseAddress or 0), info.RegionSize
        a = (info.BaseAddress or 0) + info.RegionSize
def run():
    L = reader.layout
    for i in range(4):
        rec = reader._read_exact(CARDS + i * L.cards.record_bytes, L.cards.record_bytes)
        say(f"   card {i}: thumbnail {reader._fstring(rec, 56)!r}, element {reader._fstring(rec, L.cards.element_offset)!r}, damage {reader._fstring(rec, L.cards.damage_offset)!r}")
    needle = CARDS.to_bytes(8, "little").rstrip(b"\0")
    for alloc in ALLOCS:
        regs = list(regions_of(alloc))
        total = sum(s for _b, s in regs)
        t0 = time.perf_counter(); found = []
        for base, size in regs:
            data = reader._read(base, size) or b""
            p = data.find(needle)
            while p >= 0:
                if p % 8 == 0 and data[p + len(needle):p + 8] == b"\0" * (8 - len(needle)):
                    around = data[max(0, p - 32):p + 48]
                    found.append((hex(base + p), [hex(int.from_bytes(around[o:o+8], 'little')) for o in range(0, len(around) - 7, 8)]))
                p = data.find(needle, p + 1)
        say(f"   alloc {alloc:#x}: {len(regs)} region(s), {total >> 10} KB, {time.perf_counter() - t0:.2f} s: cards pointer at {found}")
    say("done")
threading.Thread(target=run, name="allocwalk", daemon=True).start()
