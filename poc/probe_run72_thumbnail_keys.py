"""All elements of RenderedThumbnails (key, value, hash links) against the equipped weapons' InstanceIds and the render target objects."""
import sys, time
from pathlib import Path
import unrealsdk
from unrealsdk import logging
from mods_base import get_pc
import dmgtracker.itemnames as itemnames
OUT = Path(__file__).parent / "probe_output.txt"
def say(m):
    logging.info("[thumbkeys] " + m)
    with OUT.open("a", encoding="utf-8") as f: f.write(m + "\n")
say(f"{time.strftime('%H:%M:%S')} thumbnail keys")
reader = itemnames.ItemNameReader()
pawn = get_pc().Pawn
ids = {}
for slot in pawn.EquippedInventorySlots.items if hasattr(pawn.EquippedInventorySlots, "items") else []:
    pass
try:
    for index, entry in enumerate(pawn.ActiveWeapons.Slots):
        weapon = entry.Weapon
        if weapon is not None:
            ids[int(weapon.Item.data.InstanceId)] = f"active slot {index} {weapon.BodyData.Name}"
except Exception as ex:
    say(f"   active weapons: {ex!r}")
try:
    for index, entry in enumerate(pawn.EquippedInventorySlots.items):
        item = getattr(entry, "Item", None) or getattr(entry, "Instance", None)
        say(f"   equipped slot {index}: {entry!r}"[:300])
except Exception as ex:
    say(f"   equipped slots: {ex!r}")
say(f"   instance ids of the active weapons: {ids}")
manager = next(o for o in unrealsdk.find_all("OakThumbnailManager", exact=True) if not str(o.Name).startswith("Default__"))
base = int(manager._get_address())
objects_by_address = {}
for cls_name in ("TextureRenderTarget2D", "Texture2D"):
    for o in unrealsdk.find_all(cls_name, exact=True):
        objects_by_address[int(o._get_address())] = f"{o.Class.Name} {o.Name}"
raw = reader._read_exact(base, 256)
for offset in (72, 152):
    ptr = int.from_bytes(raw[offset:offset+8], "little"); num = int.from_bytes(raw[offset+8:offset+12], "little", signed=True)
    block = reader._read(ptr, num * 24) or b""
    say(f"   map at +{offset}: {num} element(s) at {ptr:#x}")
    for i in range(num):
        e = block[i*24:(i+1)*24]
        key_a, key_b = int.from_bytes(e[0:4], "little"), int.from_bytes(e[4:8], "little")
        value = int.from_bytes(e[8:16], "little")
        say(f"      {i}: key ({key_a}, {key_b}) value {value:#x} -> {objects_by_address.get(value, '?')}; links {int.from_bytes(e[16:20], 'little', signed=True)}/{int.from_bytes(e[20:24], 'little', signed=True)}; {ids.get(key_b, '')}")
say("done")
