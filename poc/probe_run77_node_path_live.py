"""The rewritten ItemNameReader live: one pass for the seed and the cards' node, the confirming read, the names; timing from the reader's own log."""
import importlib, sys, time, threading
from pathlib import Path
from unrealsdk import logging
from mods_base import get_pc
import dmgtracker.itemnames as itemnames
from dmgtracker.itemidentity import seed_of
OUT = Path(__file__).parent / "probe_output.txt"
def say(m):
    logging.info("[nodereader2] " + m)
    with OUT.open("a", encoding="utf-8") as f: f.write(m + "\n")
say(f"{time.strftime('%H:%M:%S')} node reader live, slot 0 with a seed no list holds")
itemnames = importlib.reload(itemnames)
mod = sys.modules["dmgtracker.mod"]
equipped = mod.resolver.equipped_weapons(get_pc().Pawn) or []
slot_seeds = [seed_of(w.item_id) for w in equipped]; slot_seeds[0] = bytes(range(20, 32))
say(f"   slots: {[(w.label, (s or b'').hex()[:6]) for w, s in zip(equipped, slot_seeds)]}")
reader = itemnames.ItemNameReader(log=lambda m: say("   reader: " + m))
t0 = time.perf_counter()
first = reader.names_for(slot_seeds)
say(f"   first call: {len(first)} name(s), discovering {reader.discovering}")
def wait():
    while reader.discovering:
        time.sleep(0.2)
    say(f"   scan over after {time.perf_counter() - t0:.1f} s; header {hex(reader._cards_header or 0)}, list {hex(reader._list_address or 0)}, error {reader.last_error!r}")
    second = reader.names_for(slot_seeds)
    for w, s in zip(equipped, slot_seeds):
        say(f"      {w.label} {(s or b'').hex()[:6]} -> {second.get(s)!r}")
    say("done")
threading.Thread(target=wait, name="nodereader", daemon=True).start()
