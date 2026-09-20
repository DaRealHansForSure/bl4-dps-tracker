"""
Phase 0 probe #16: what fires when an action skill is used, and does it run the offhand trick?

The Exo-Soldier's Schnitter skill was credited to Melee: the melee trick (HandleOffhand,
under Tricks/Skills/) may run for off-hand skills too. This probe prints every trick that
begins on the player's character and every skill script event on the player, with the
script's class path, so the action skill's events can be told from passives and from the
knife.

    pyexec probe_skills.py    start, then: use the action skill once (no knife), wait for it
                              to end, then knife once
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
HOOK_ID = "dmgskills"
HOOKED_FUNCTIONS = (
    "/Script/GbxGame.GbxTrickScript:OnBegin_Mut",
    "/Script/GbxGame.GbxTrickScript:OnEnd_Mut",
    "/Script/GbxGame.GbxSkillScript:FireSkillEvent",
    "/Script/GbxGame.GbxSkillScript:K2_SkillStateBranch",
    "/Script/OakGame.GbxSkillTokenProviderStatics:AddToTokenStack",
    "/Script/GbxGame.GbxSkillBlueprintPrivate:CallSkillMessageInternal_FiveParams",
)
OWNER_GETTERS = ("GetAssociatedActor", "GetOwner", "GetSkillOwner", "GetOwningActor")

state: dict[str, Any] = {"hooked": [], "start": 0.0}


def say(msg: str) -> None:
    logging.info(f"[dmgskills] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def player_pawn_address() -> int:
    try:
        pawn = get_pc().Pawn
        return int(pawn._get_address()) if pawn is not None else 0
    except Exception:  # noqa: BLE001
        return 0


def owner_of(obj: UObject) -> tuple[str, str]:
    """(getter name, description) for the first owner getter that answers."""
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
        return getter, f"{actor.Class.Name} {'(player)' if mine else '(other)'}"
    return "", "unknown owner"


def on_call(obj: UObject, args: WrappedStruct, ret: Any, func: BoundFunction) -> None:
    try:
        getter, owner = owner_of(obj)
        if "(other)" in owner:
            return
        class_path = str(obj.Class._path_name())
        say(f"+{time.time() - state['start']:7.2f}s {func.func.Name:34} {class_path.rsplit('/', 1)[-1][:70]:70} owner={owner} via {getter or '-'}")
    except Exception as ex:  # noqa: BLE001
        say(f"call error: {type(ex).__name__}: {ex}")


def stop() -> None:
    for path in state["hooked"]:
        try:
            if hooks.has_hook(path, hooks.Type.PRE, HOOK_ID):
                hooks.remove_hook(path, hooks.Type.PRE, HOOK_ID)
        except Exception:  # noqa: BLE001
            pass
    state["hooked"].clear()


old_stop = getattr(builtins, "_dmgskills_stop", None)
if old_stop is not None:
    old_stop()
builtins._dmgskills_stop = stop  # type: ignore[attr-defined]

say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} skill probe")
state["start"] = time.time()
for path in HOOKED_FUNCTIONS:
    try:
        unrealsdk.find_object("Function", path)
    except ValueError:
        say(f"   not found: {path}")
        continue
    hooks.add_hook(path, hooks.Type.PRE, HOOK_ID, on_call)
    state["hooked"].append(path)
    say(f"   hooked {path}")
say("use the action skill once without knifing, wait for it to end, then knife once; then `pyexec probe_stop.py`")
