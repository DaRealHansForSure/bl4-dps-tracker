"""The raw memory of OakThumbnailManager's maps: find the render target pointers inside the element arrays and show the key bytes before them; compare with the equipped seeds."""
import sys, time
from pathlib import Path
import unrealsdk
from unrealsdk import logging
from mods_base import get_pc
import dmgtracker.itemnames as itemnames
OUT = Path(__file__).parent / "probe_output.txt"
def say(m):
    logging.info("[thumbmap] " + m)
    with OUT.open("a", encoding="utf-8") as f: f.write(m + "\n")
say(f"{time.strftime('%H:%M:%S')} thumbnail map raw")
reader = itemnames.ItemNameReader()
mod = sys.modules["dmgtracker.mod"]
equipped = mod.resolver.equipped_weapons(get_pc().Pawn) or []
seeds = {w.item_id: w.label for w in equipped}
manager = next(o for o in unrealsdk.find_all("OakThumbnailManager", exact=True) if not str(o.Name).startswith("Default__"))
base = int(manager._get_address())
rts = {int(o._get_address()): str(o.Name) for o in unrealsdk.find_all("TextureRenderTarget2D", exact=True) if str(o.Name).startswith("OakInventoryThumbnail_")}
say(f"   manager {base:#x}; {len(rts)} OakInventoryThumbnail render targets")
raw = reader._read_exact(base, 256)
words = [int.from_bytes(raw[o:o+8], "little") for o in range(0, 256, 8)]
say("   words: " + " ".join(f"{o*8}:{w:#x}" for o, w in enumerate(words)))
# TSparseArray data headers: a pointer followed by num, max where max >= num > 0
for o in range(0, 240, 8):
    ptr = int.from_bytes(raw[o:o+8], "little"); num = int.from_bytes(raw[o+8:o+12], "little", signed=True); mx = int.from_bytes(raw[o+12:o+16], "little", signed=True)
    if not itemnames._is_pointer(ptr) or num <= 0 or num > 4096 or mx < num: continue
    for element_bytes in (24, 32, 40, 48, 56, 64, 72, 80, 96, 112, 128):
        block = reader._read(ptr, num * element_bytes)
        if block is None or len(block) < num * element_bytes: continue
        hits = [(i, rts[int.from_bytes(block[i:i+8], "little")]) for i in range(0, len(block) - 7, 8) if int.from_bytes(block[i:i+8], "little") in rts]
        if hits and len({i % element_bytes for i, _n in hits}) == 1:
            say(f"   header at +{o}: data {ptr:#x} num {num} max {mx}; element size {element_bytes} puts every RT pointer at offset {hits[0][0] % element_bytes}; {len(hits)} RT(s)")
            for i, name in hits[:6]:
                start = i - i % element_bytes
                element = block[start:start + element_bytes]
                seed_hits = [seeds[s] for s in seeds if bytes.fromhex(s) in element]
                say(f"      element {start // element_bytes}: {element.hex()} -> {name}; seeds inside: {seed_hits}")
            break
say("done")
