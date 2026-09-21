"""The 128 bytes around the cards' TArray header found by probe 74, to compare the node with session 3's."""
import time
from pathlib import Path
from unrealsdk import logging
import dmgtracker.itemnames as itemnames
OUT = Path(__file__).parent / "probe_output.txt"
def say(m):
    logging.info("[headerctx] " + m)
    with OUT.open("a", encoding="utf-8") as f: f.write(m + "\n")
HEADER = 0x31f312510
reader = itemnames.ItemNameReader()
raw = reader._read_exact(HEADER - 64, 128)
words = [f"{o - 64:+d}:{int.from_bytes(raw[o:o+8], 'little'):#x}" for o in range(0, 128, 8)]
say(f"{time.strftime('%H:%M:%S')} around header {HEADER:#x}: " + " ".join(words))
elements = [reader._read_exact(int.from_bytes(raw[o:o+8], 'little'), 32) for o in (64 - 16,)]
say(f"   the {{ptr,1,1}} array before it: element bytes {elements[0].hex() if elements[0] else None}")
say("done")
