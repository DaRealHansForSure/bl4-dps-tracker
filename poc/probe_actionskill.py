"""
Phase 0 probe #17: a general "action skill is running" signal, for every character.

The mod credits skill damage from two signals: tricks whose class name contains "ActionSkill"
(Siren) and `GbxSkillTokenProviderStatics:AddToTokenStack` (RoboDealer tokens). A different
RoboDealer skill fires neither. The class list shows skill function libraries per character
(`GbxSkillComponentFunctions_ActionSkill`, `..._RoboDealer`, `..._Exo`, `..._Paladin`, ...),
so a base-library function that every action skill runs on start and end would be the
signal for all of them.

Step 1 (no hooks): list every function of the skill-related classes with its parameters.
Step 2: hook all of them (PRE, no argument reads) and print each call with its time.

    pyexec probe_actionskill.py     then use the new action skill on enemies for ~10 s,
                                    and once more the token skill if it is equipped
    pyexec probe_stop.py            stop

Prints to the console and to probe_output.txt. Reads no hook arguments.
"""

from __future__ import annotations

import builtins
import re
import time
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import get_pc
from unrealsdk import hooks, logging
from unrealsdk.unreal import BoundFunction, UClass, UFunction, UObject, WrappedStruct

OUT = Path(__file__).parent / "probe_output.txt"
HOOK_ID = "dmgactionskill"
CLASS_PATTERN = re.compile(r"ActionSkill|SkillComponent|SkillTokenProvider|GbxSkillScript$|GbxSkill$|SkillManager|AbilityComponent", re.IGNORECASE)
MAX_HOOKS = 200
PRINTS_PER_FUNCTION = 25
THEN_EVERY = 50
OWNER_GETTERS = ("GetOwner", "GetSkillOwner", "GetAssociatedActor", "GetOwningActor")

state: dict[str, Any] = {"hooked": [], "counts": {}, "start": 0.0}


def say(msg: str) -> None:
    logging.info(f"[dmgactionskill] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def own_functions(cls: UClass) -> list[UFunction]:
    """The functions declared on this class itself, not the inherited ones."""
    functions: list[UFunction] = []
    for field in cls._fields():
        if isinstance(field, UFunction) and int(field.Outer._get_address()) == int(cls._get_address()):
            functions.append(field)
    return functions


def signature(func: UFunction) -> str:
    parts: list[str] = []
    for prop in func._properties():
        parts.append(f"{prop.Name}:{str(prop.Class.Name).replace('Property', '')}")
    return ", ".join(parts)


def player_pawn_address() -> int:
    try:
        pawn = get_pc().Pawn
        return int(pawn._get_address()) if pawn is not None else 0
    except Exception:  # noqa: BLE001
        return 0


def describe_owner(obj: UObject) -> str:
    """Who the call runs on: the class default object of a static library, or an instance
    with its owner (player or other) when a no-argument getter says so."""
    try:
        if str(obj.Name).startswith("Default__"):
            return "static"
        for getter in OWNER_GETTERS:
            if not hasattr(obj, getter):
                continue
            try:
                actor = getattr(obj, getter)()
            except Exception:  # noqa: BLE001
                continue
            if actor is None:
                continue
            mine = int(actor._get_address()) == player_pawn_address()
            return f"{obj.Class.Name} of {actor.Class.Name} {'(player)' if mine else '(other)'}"
        return f"{obj.Class.Name} instance"
    except Exception as ex:  # noqa: BLE001
        return f"<owner err {type(ex).__name__}>"


def on_call(obj: UObject, args: WrappedStruct, ret: Any, func: BoundFunction) -> None:
    try:
        path = str(func.func._path_name())
        count = state["counts"].get(path, 0) + 1
        state["counts"][path] = count
        if count > PRINTS_PER_FUNCTION and count % THEN_EVERY != 0:
            return
        say(f"+{time.time() - state['start']:7.2f}s t={time.time():.3f} #{count:<4d} {path.rsplit('.', 1)[-1]:60} {describe_owner(obj)}")
    except Exception as ex:  # noqa: BLE001
        say(f"call error: {type(ex).__name__}: {ex}")


def stop() -> None:
    for path in state["hooked"]:
        try:
            if hooks.has_hook(path, hooks.Type.PRE, HOOK_ID):
                hooks.remove_hook(path, hooks.Type.PRE, HOOK_ID)
        except Exception:  # noqa: BLE001
            pass
    say(f"   removed {len(state['hooked'])} hooks; calls seen:")
    for path, count in sorted(state["counts"].items(), key=lambda item: -item[1]):
        say(f"   {count:6d}  {path}")
    state["hooked"].clear()
    state["counts"].clear()


old_stop = getattr(builtins, "_dmgactionskill_stop", None)
if old_stop is not None:
    old_stop()
builtins._dmgactionskill_stop = stop  # type: ignore[attr-defined]

say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} action skill probe")
state["start"] = time.time()

matching: list[UClass] = []
for cls in unrealsdk.find_all("Class", exact=True):
    if CLASS_PATTERN.search(str(cls.Name)):
        matching.append(cls)
say(f"{len(matching)} skill-related classes")
to_hook: list[str] = []
for cls in sorted(matching, key=lambda item: str(item.Name)):
    functions = own_functions(cls)
    parent = cls.SuperField
    say(f"class {cls._path_name()} (super {parent.Name if parent is not None else '-'}): {len(functions)} own functions")
    for func in functions:
        say(f"      {func.Name}({signature(func)})")
        if len(to_hook) < MAX_HOOKS:
            to_hook.append(str(func._path_name()))

for path in to_hook:
    try:
        hooks.add_hook(path, hooks.Type.PRE, HOOK_ID, on_call)
        state["hooked"].append(path)
    except Exception as ex:  # noqa: BLE001
        say(f"   cannot hook {path}: {type(ex).__name__}: {ex}")
say(f"hooked {len(state['hooked'])} functions; use the action skill now, then `pyexec probe_stop.py`")
