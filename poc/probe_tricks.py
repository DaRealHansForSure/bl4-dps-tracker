"""
Phase 0 probe #15: which trick script runs a melee swing, per character?

The Rogue's knife runs the shared `Script_LTrick_HandleOffhand` trick, but the hook on it
did not fire for the Exo-Soldier. Every trick, whatever its script, starts through the
native `/Script/GbxGame.GbxTrickScript:OnBegin_Mut`; this probe hooks that and prints each
trick that begins on the player's own character, so the melee trick's name can be read off.

    pyexec probe_tricks.py    start, then knife a few times, sprint, jump, swap weapons
    pyexec probe_stop.py      stop

Prints to the console and to probe_output.txt. Reads no hook arguments.
"""

from __future__ import annotations

import builtins
import time
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import get_pc
from unrealsdk import hooks, logging
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

OUT = Path(__file__).parent / "probe_output.txt"
HOOK_ID = "dmgtricks"
TRICK_FUNCTIONS = (
    "/Script/GbxGame.GbxTrickScript:OnBegin_Mut",
    "/Script/GbxGame.GbxTrickScript:OnBegin_Const",
    "/Script/GbxGame.GbxTrickScript:OnAnimNotify",
)

state: dict[str, Any] = {"hooked": [], "start": 0.0}


def say(msg: str) -> None:
    logging.info(f"[dmgtricks] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def described(fn) -> str:
    try:
        value = fn()
    except Exception as ex:  # noqa: BLE001
        return f"<err {type(ex).__name__}: {ex}>"
    if isinstance(value, UObject):
        return f"{value._path_name()} ({value.Class.Name})"
    return repr(value)[:200]


def is_player_actor(actor: Any) -> bool:
    try:
        pawn = get_pc().Pawn
        return pawn is not None and int(pawn._get_address()) == int(actor._get_address())
    except Exception:  # noqa: BLE001
        return False


def on_trick(obj: UObject, args: WrappedStruct, ret: Any, func: BoundFunction) -> None:
    try:
        try:
            actor = obj.GetAssociatedActor()
        except Exception:  # noqa: BLE001
            actor = None
        if actor is None or not is_player_actor(actor):
            return
        say(f"+{time.time() - state['start']:7.2f}s {func.func.Name:14} {obj.Class.Name} <- {obj._path_name().rsplit('.', 1)[-1]}")
    except Exception as ex:  # noqa: BLE001
        say(f"trick error: {type(ex).__name__}: {ex}")


def stop() -> None:
    for path in state["hooked"]:
        try:
            if hooks.has_hook(path, hooks.Type.PRE, HOOK_ID):
                hooks.remove_hook(path, hooks.Type.PRE, HOOK_ID)
        except Exception:  # noqa: BLE001
            pass
    state["hooked"].clear()


old_stop = getattr(builtins, "_dmgtricks_stop", None)
if old_stop is not None:
    old_stop()
builtins._dmgtricks_stop = stop  # type: ignore[attr-defined]

say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} trick probe")
state["start"] = time.time()
for path in TRICK_FUNCTIONS:
    try:
        unrealsdk.find_object("Function", path)
    except ValueError:
        say(f"   not found: {path}")
        continue
    hooks.add_hook(path, hooks.Type.PRE, HOOK_ID, on_trick)
    state["hooked"].append(path)
    say(f"   hooked {path}")
say("knife a few times, then sprint, jump and swap weapons for comparison; then `pyexec probe_stop.py`")
