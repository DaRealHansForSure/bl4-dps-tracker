"""
Phase 0 probe #14: what identifies the current character (save slot), stably across launches?

Per-character stats need a key that stays the same for one save and differs between two
saves of the same class. Candidates: the player state, the pawn's PlayerCharacterState, and
anything on the controller or pawn whose name mentions Save, Slot, Guid, Id, Name or
Character. Names only, values for the matching ones.

    pyexec probe_character.py

Prints to the console and to probe_output.txt. Run it with each character once.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from mods_base import get_pc
from unrealsdk import logging
from unrealsdk.unreal import UObject

OUT = Path(__file__).parent / "probe_output.txt"
MAX_VALUE_CHARS = 240
IDENTITY_PATTERN = re.compile(r"Save|Slot|Guid|Id$|ID$|Name|Character|Profile|Serial", re.IGNORECASE)
MAX_STRUCT_DEPTH = 2


def say(msg: str) -> None:
    logging.info(f"[dmgchar] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def described(fn) -> str:
    try:
        value = fn()
    except Exception as ex:  # noqa: BLE001
        return f"<err {type(ex).__name__}: {ex}>"
    if isinstance(value, UObject):
        return f"{value._path_name()} ({value.Class.Name})"
    return repr(value)[:MAX_VALUE_CHARS]


def dump_identity_fields(label: str, obj: Any, depth: int = 1) -> None:
    """Values of the identity-looking fields of an object or struct, nested structs included."""
    try:
        properties = list(obj.Class._properties()) if isinstance(obj, UObject) else list(obj._type._properties())
    except Exception as ex:  # noqa: BLE001
        say(f"   {label}: cannot list fields: {type(ex).__name__}: {ex}")
        return
    for prop in properties:
        name = str(prop.Name)
        prop_class = str(prop.Class.Name)
        if "Delegate" in prop_class:
            continue
        if IDENTITY_PATTERN.search(name):
            say(f"   {label}.{name}: {prop_class} = {described(lambda: getattr(obj, name))}")
        if prop_class == "StructProperty" and depth < MAX_STRUCT_DEPTH and IDENTITY_PATTERN.search(name):
            try:
                nested = getattr(obj, name)
            except Exception:  # noqa: BLE001
                continue
            dump_identity_fields(f"{label}.{name}", nested, depth + 1)


say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} character probe")
pc = get_pc()
pawn = pc.Pawn
say(f"   controller: {described(lambda: pc)}")
say(f"   pawn: {described(lambda: pawn)}")
say(f"   pawn def: {described(lambda: pawn.GbxActorData.GbxActorDef)}")
say(f"   player state: {described(lambda: pc.PlayerState)}")
say(f"   player name: {described(lambda: pc.PlayerState.GetPlayerName())}")

say("step 1/4: controller identity fields")
dump_identity_fields("pc", pc)
say("step 2/4: player state identity fields")
dump_identity_fields("state", pc.PlayerState)
say("step 3/4: pawn identity fields")
dump_identity_fields("pawn", pawn)
say("step 4/4: PlayerCharacterState")
try:
    dump_identity_fields("pawn.PlayerCharacterState", pawn.PlayerCharacterState)
    say(f"   raw: {described(lambda: repr(pawn.PlayerCharacterState))}")
except Exception as ex:  # noqa: BLE001
    say(f"   PlayerCharacterState: {type(ex).__name__}: {ex}")
say("done")
