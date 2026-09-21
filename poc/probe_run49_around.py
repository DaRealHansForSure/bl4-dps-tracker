"""What lies around this session's identity copies (seed hits 0x329d8930, 0x32088ed38, 0x33fcf3970): strings, seeds, card candidates."""
import struct, time
from pathlib import Path
from unrealsdk import logging
import dmgtracker.itemnames as itemnames
OUT = Path(__file__).parent / "probe_output.txt"
def say(m):
    logging.info("[around] " + m)
    with OUT.open("a", encoding="utf-8") as f: f.write(m + "\n")
reader = itemnames.ItemCardReader()
read = reader._read
SEEDS = {"ddbe5d456a2ec99a4c917f15": "shotgun", "9bf02ca566b1c0010e1227cd": "AR", "134844c299550a25ddaa1fe6": "SMG", "4403462d66781a89a0eccea0": "pistol"}
def fstring(d, o):
    return reader._fstring(d, o)
say(f"{time.strftime('%H:%M:%S')} around the seed hits")
for hit in (0x329d8930, 0x32088ed38, 0x33fcf3970, 0xe28a018):
    start = hit - 2048
    d = read(start, 6144)
    if not d:
        say(f"   {hit:#x}: unreadable"); continue
    say(f"   hit {hit:#x}: region {start:#x}..{start + len(d):#x}")
    for seed, label in SEEDS.items():
        p = d.find(bytes.fromhex(seed))
        while p >= 0:
            say(f"      {start + p:#x} ({p - 2048:+d}): seed of {label}")
            p = d.find(bytes.fromhex(seed), p + 1)
    for o in range(0, len(d) - 15, 8):
        t = fstring(d, o)
        if t:
            say(f"      {start + o:#x} ({o - 2048:+d}): FString {t!r}")
    # card candidates: any offset where _card_at validates
    for o in range(0, len(d) - 280, 8):
        card = reader._card_at(start + o)
        if card:
            say(f"      {start + o:#x} ({o - 2048:+d}): CARD {card.name!r} {card.serial[:10]} {card.rarity}")
say("done")
