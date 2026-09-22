"""Is there an equip / weapon-change event the mod could hook instead of polling the
equipment once a second?

Phase 1 (reflection, instant): every UFunction of the pawn's, the controller's, the
weapon's and the inventory classes whose name looks like an equipment change, with the
path an `add_hook` would take.

Phase 2 (live, 180 s): PRE hooks on those candidates, logging the first few firings of
each with the calling object's class, so a swap in the game shows which of them actually
fire. Hooks read nothing but the object (reading an argument of an unknown type crashed
the game once) and are all removed when the window is over.

Swap guns a few times while it runs, equip a gun from the ground, open and close the
inventory.
"""
import re, sys, time, threading
from pathlib import Path
import unrealsdk
from unrealsdk import logging
from unrealsdk.hooks import Type
from mods_base import get_pc

OUT = Path(__file__).parent / "probe_output.txt"
HOOK_ID = "probe84-equip"
WINDOW_S = 180.0
MAX_CANDIDATES = 48
MAX_LINES_PER_FUNCTION = 3
NAME_PATTERN = re.compile(
    r"equip|unequip|swap|switch|holster|wield|activeweapon|weaponchang|itemchang|"
    r"inventorychang|slotchang|onslot|onitem|onweapon|oninventory|setweapon|readyweapon|putaway",
    re.IGNORECASE,
)


def say(m):
    logging.info("[equip84] " + m)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(m + "\n")


def path_of(obj):
    try:
        return obj._path_name()
    except Exception:
        return str(obj)


def class_chain(cls):
    """A class and its superclasses, outermost last."""
    chain = []
    seen = set()
    while cls is not None and int(cls._get_address()) not in seen:
        seen.add(int(cls._get_address()))
        chain.append(cls)
        cls = getattr(cls, "SuperField", None) or getattr(cls, "Super", None)
    return chain


def functions_of(cls):
    """(name, path) of every UFunction declared on the class itself."""
    found = []
    try:
        fields = list(cls._fields())
    except Exception as ex:
        say(f"      cannot list the fields of {path_of(cls)}: {type(ex).__name__}: {ex}")
        return found
    for field in fields:
        try:
            if str(field.Class.Name) != "Function":
                continue
            name = str(field.Name)
            found.append((name, f"{path_of(cls)}:{name}"))
        except Exception:
            continue
    return found


def collect_classes():
    """The classes worth looking at, from the live objects and by name."""
    classes = {}

    def add(cls, why):
        if cls is None:
            return
        for step in class_chain(cls):
            key = path_of(step)
            if key not in classes:
                classes[key] = (step, why)

    controller = get_pc()
    pawn = getattr(controller, "Pawn", None)
    add(getattr(controller, "Class", None), "the player controller")
    add(getattr(pawn, "Class", None), "the pawn")
    mod = sys.modules.get("dmgtracker.mod")
    if mod is not None and pawn is not None:
        held = None
        try:
            held = pawn.ActiveWeapons.Slots[0].Weapon
        except Exception:
            pass
        add(getattr(held, "Class", None), "the weapon in hand")
        try:
            for item in pawn.EquippedInventorySlots.items:
                add(getattr(item.InstancedInventory, "Class", None), "an equipped item")
        except Exception as ex:
            say(f"   equipped items unreadable: {type(ex).__name__}: {ex}")
    for name in ("OakCharacter", "OakPlayerController", "OakInventoryManager", "OakInventory",
                 "GbxInventoryManager", "OakEquippedInventoryComponent", "OakWeapon", "GrenadeGadget",
                 "OakInventoryItemPickup", "OakPlayerInventoryComponent", "InventoryManager"):
        try:
            add(unrealsdk.find_class(name), f"find_class({name})")
        except Exception:
            continue
    return classes


def phase_one():
    classes = collect_classes()
    say(f"   {len(classes)} class(es) to look at")
    candidates = []
    for key, (cls, why) in classes.items():
        functions = functions_of(cls)
        matches = [(name, path) for name, path in functions if NAME_PATTERN.search(name)]
        say(f"   {key} ({why}): {len(functions)} function(s), {len(matches)} matching")
        for name, path in matches:
            say(f"      {path}")
            candidates.append(path)
    return candidates[:MAX_CANDIDATES]


_counts = {}
_lock = threading.Lock()


def make_callback(path):
    def callback(obj, args, ret, func):
        try:
            with _lock:
                seen = _counts.get(path, 0)
                _counts[path] = seen + 1
            if seen < MAX_LINES_PER_FUNCTION:
                try:
                    what = str(obj.Class.Name)
                except Exception:
                    what = "?"
                say(f"      {time.strftime('%H:%M:%S')} FIRED {path} on {what}")
        except Exception:
            pass
    return callback


def phase_two(candidates):
    installed = []
    for index, path in enumerate(candidates):
        identifier = f"{HOOK_ID}:{index}"
        try:
            unrealsdk.hooks.add_hook(path, Type.PRE, identifier, make_callback(path))
            installed.append((path, identifier))
        except Exception as ex:
            say(f"   cannot hook {path}: {type(ex).__name__}: {ex}")
    say(f"   {len(installed)} hook(s) installed; swap guns, equip one from the ground, open the inventory ({WINDOW_S:.0f} s)")

    def finish():
        time.sleep(WINDOW_S)
        for path, identifier in installed:
            try:
                if unrealsdk.hooks.has_hook(path, Type.PRE, identifier):
                    unrealsdk.hooks.remove_hook(path, Type.PRE, identifier)
            except Exception as ex:
                say(f"   cannot remove the hook on {path}: {type(ex).__name__}: {ex}")
        say("   hooks removed; firing counts:")
        with _lock:
            counts = dict(_counts)
        if not counts:
            say("      none of the candidates fired")
        for path, count in sorted(counts.items(), key=lambda pair: -pair[1]):
            say(f"      {count:>5} x {path}")
        say("done")

    threading.Thread(target=finish, name="equip84", daemon=True).start()


say(f"{time.strftime('%H:%M:%S')} equipment events: what could be hooked")
phase_two(phase_one())
