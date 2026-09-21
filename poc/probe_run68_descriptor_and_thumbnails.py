"""Which module holds the binding descriptor of the item cards property (a static: module base + RVA), and what the OakInventoryThumbnail objects of the cards know about their items."""
import ctypes, ctypes.wintypes as wintypes, time
from pathlib import Path
import unrealsdk
from unrealsdk import logging
OUT = Path(__file__).parent / "probe_output.txt"
def say(m):
    logging.info("[descr] " + m)
    with OUT.open("a", encoding="utf-8") as f: f.write(m + "\n")
k = ctypes.WinDLL("kernel32")
k.GetModuleHandleExW.argtypes = (wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)); k.GetModuleHandleExW.restype = wintypes.BOOL
k.GetModuleFileNameW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, wintypes.DWORD); k.GetModuleFileNameW.restype = wintypes.DWORD
def module_of(address):
    handle = ctypes.c_void_p(0)
    if not k.GetModuleHandleExW(0x4 | 0x2, ctypes.c_void_p(address), ctypes.byref(handle)): return None, 0
    buf = ctypes.create_unicode_buffer(520); k.GetModuleFileNameW(handle, buf, 520)
    return buf.value, handle.value or 0
say(f"{time.strftime('%H:%M:%S')} descriptor module and thumbnails")
for label, a in (("cards descriptor", 0x7fffa88dc578), ("prev descriptor", 0x7fffa88dc558), ("next descriptor", 0x7fffa88dc598), ("map typeinfo", 0x7fffa87ea420)):
    name, base = module_of(a)
    say(f"   {label} {a:#x}: module {name}, base {base:#x}, RVA {a - base:#x}" if base else f"   {label} {a:#x}: no module")
cls = unrealsdk.find_class("OakInventoryThumbnail", exact=False) if True else None
try:
    cls = unrealsdk.find_class("OakInventoryThumbnail")
    say(f"   class {cls.Name}: super {cls.SuperField.Name if cls.SuperField else None}; properties {[(str(p.Name), p.Class.Name) for p in cls._properties()][:40]}")
except Exception as ex:
    say(f"   class OakInventoryThumbnail: {ex!r}")
objects = [o for o in unrealsdk.find_all("Object", exact=False) if str(o.Name).startswith("OakInventoryThumbnail_")] if False else []
wanted = {"OakInventoryThumbnail_2147475564", "OakInventoryThumbnail_2147475358", "OakInventoryThumbnail_2147475260", "OakInventoryThumbnail_2147475745"}
found = []
for candidate_class in ("TextureRenderTarget2D", "Object"):
    try:
        for o in unrealsdk.find_all(candidate_class, exact=(candidate_class != "Object")):
            if str(o.Name) in wanted:
                found.append(o)
        if found: break
    except Exception as ex:
        say(f"   find_all {candidate_class}: {ex!r}")
say(f"   thumbnail objects: {[(str(o.Name), str(o.Class.Name), o._path_name() if hasattr(o, '_path_name') else '') for o in found]}")
for o in found[:2]:
    try:
        outer = o.Outer
        say(f"      {o.Name}: outer {outer.Name if outer else None} ({outer.Class.Name if outer else None}); outer's outer {outer.Outer.Name if outer and outer.Outer else None}")
        props = [(str(p.Name), p.Class.Name) for p in outer.Class._properties()] if outer else []
        say(f"      outer properties: {props[:40]}")
    except Exception as ex:
        say(f"      {o.Name}: {ex!r}")
say("done")
