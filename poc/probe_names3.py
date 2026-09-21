"""
Phase 0 probe #18: which function of the game itself gives an item its display name?

The game shows "Lionized Parasite" in the inventory, so somewhere a native function builds
that text from the item. The earlier probes looked at the weapon actor, the item structs and
a few libraries; this one sweeps every UFunction in the process for the ones that can hand
out a name: text or string results, or text out-parameters, on anything item-, inventory-
or UI-related, plus every class of the UI modules (the Coherent HUD, the UI scripts) with
its functions. Definitions only, no calls, no hooks: cheap and safe.

    pyexec probe_names3.py

Prints to the console and to probe_output.txt.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import unrealsdk
from unrealsdk import logging
from unrealsdk.unreal import UFunction

OUT = Path(__file__).parent / "probe_output.txt"
TEXT_KINDS = ("TextProperty", "StrProperty")
NAME_PATTERN = re.compile(r"Name|Title|Label|Text|Display|Card|Tooltip|Describ|Summary|Caption|Header", re.IGNORECASE)
OWNER_PATTERN = re.compile(r"UI|Inventory|Item|Weapon|Gear|Nexus|Loot|Pickup|Equip|Backpack|Vending|Shop|Trade", re.IGNORECASE)
IGNORE_PATTERN = re.compile(r"Subtitle|SaveGame|Server|Client|Multicast|OnRep|Debug|Editor|Console", re.IGNORECASE)
UI_MODULES = ("/Script/GbxUICoherent", "/Script/GbxUI.", "/Script/Cohtml", "/Script/GbxUIUMG")
MAX_LINES = 900


def say(msg: str) -> None:
    logging.info(f"[dmgnames3] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def param_text(prop: Any) -> str:
    kind = str(prop.Class.Name).replace("Property", "")
    detail = ""
    try:
        if kind == "Struct":
            detail = f"<{prop.Struct.Name}>"
        elif kind == "Object":
            detail = f"<{prop.PropertyClass.Name}>"
        elif kind == "Array":
            detail = f"<{str(prop.Inner.Class.Name).replace('Property', '')}>"
    except Exception:  # noqa: BLE001
        detail = ""
    flags = ""
    try:
        if prop.PropertyFlags & 0x400:  # CPF_ReturnParm
            flags = "=>"
        elif prop.PropertyFlags & 0x100:  # CPF_OutParm
            flags = "out "
    except Exception:  # noqa: BLE001
        pass
    return f"{flags}{prop.Name}:{kind}{detail}"


def signature(func: UFunction) -> str:
    return ", ".join(param_text(prop) for prop in func._properties())


def hands_out_text(func: UFunction) -> bool:
    for prop in func._properties():
        if str(prop.Class.Name) in TEXT_KINDS:
            try:
                if prop.PropertyFlags & (0x400 | 0x100):
                    return True
            except Exception:  # noqa: BLE001
                return True
    return False


def takes_an_object_or_handle(func: UFunction) -> bool:
    for prop in func._properties():
        kind = str(prop.Class.Name)
        if kind in ("ObjectProperty", "GameDataHandleProperty", "InterfaceProperty", "StructProperty", "GbxDefPtrProperty"):
            return True
    return False


def sweep_functions() -> None:
    say("step 1/2: functions that hand out text (return or out parameter) on item, inventory or UI classes")
    lines = 0
    for func in unrealsdk.find_all("Function", exact=True):
        try:
            owner = str(func.Outer._path_name())
            name = str(func.Name)
            if IGNORE_PATTERN.search(name) or IGNORE_PATTERN.search(owner):
                continue
            if not hands_out_text(func):
                continue
            relevant = OWNER_PATTERN.search(owner) or NAME_PATTERN.search(name)
            if not relevant or not takes_an_object_or_handle(func):
                continue
            say(f"   {owner.rsplit('/', 1)[-1]}:{name}({signature(func)})")
            lines += 1
            if lines >= MAX_LINES:
                say("   ... stopped at the line limit")
                return
        except Exception as ex:  # noqa: BLE001
            say(f"   <err {type(ex).__name__}: {ex}>")
    say(f"   {lines} function(s)")


def sweep_ui_classes() -> None:
    say("step 2/2: classes of the UI modules and their functions")
    for cls in sorted(unrealsdk.find_all("Class", exact=True), key=lambda item: str(item._path_name())):
        path = str(cls._path_name())
        if not any(path.startswith(module) for module in UI_MODULES):
            continue
        functions = [field for field in cls._fields() if isinstance(field, UFunction) and int(field.Outer._get_address()) == int(cls._get_address())]
        names = ", ".join(f"{func.Name}({signature(func)})" for func in functions)
        say(f"   {path} ({len(functions)} fn): {names[:1500]}")


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} names probe v3")
for step in (sweep_functions, sweep_ui_classes):
    try:
        step()
    except Exception as ex:  # noqa: BLE001
        say(f"   step failed: {type(ex).__name__}: {ex}")
say("done")
