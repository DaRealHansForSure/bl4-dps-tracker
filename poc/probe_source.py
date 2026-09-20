"""
Phase 0 probe #13 (v2): is there a real link from a damage instance to the weapon that caused it?

v1 established that the ring entry's DamageSource container is always empty, and that the
Torgue gun's impact rounds are OakProjectile actors whose `projectile:Explode` fires within
50 ms of the unflagged damage. v1 also crashed the game by reading the hook arguments of
ThrowGadget (reading arguments of unknown property types inside a hook is unsafe). v2 reads
no arguments at all: only the object, three no-argument getters and a few struct fields.

    pyexec probe_source.py    start
    pyexec probe_stop.py      stop

Prints to the console and to probe_output.txt. Runs alongside the mod (read-only).
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
HOOK_ID = "dmgsource"
TICK_FUNC = "/Script/GbxUIUMG.GbxUIUMGTickWidget:BP_TickWidget"
RING_PROPERTY = "RecentlyCausedDamageInstances"
RING_SIZE = 6
MAX_VALUE_CHARS = 200
WEAPON_FLAG = 1 << 10
EVENT_FUNCTIONS = (
    "/Script/GbxGame.projectile:Explode",
    "/Script/OakGame.GrenadeGadget:ExplodeCallback",
    "/Script/OakGame.OakCharacter:ThrowGadget",
    "/Script/GbxGame.LightProjectileScript:OnImpact",
    "/Game/PlayerCharacters/_Shared/Tricks/Skills/Script_LTrick_HandleOffhand.Script_LTrick_HandleOffhand_C:OnBegin_Const",
)
GETTERS = ("GetOwner", "GetInstigator", "GetInstigatorController", "GetAssociatedActor")
STRUCT_FIELDS = ("ScriptData", "TrickData", "GbxActorData", "SourceActor", "DamageBalanceData", "DamageType")


def say(msg: str) -> None:
    logging.info(f"[dmgsource] {msg}")
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


state: dict[str, Any] = {"dirty": [-1] * RING_SIZE, "hooked": [], "start": 0.0}


def stamp() -> str:
    return f"+{time.time() - state['start']:7.2f}s"


def weapon_in_hand() -> str:
    try:
        pawn = get_pc().Pawn
        weapon = pawn.ActiveWeapons.Slots[0].Weapon
        return f"{weapon.BodyData._path_name().rsplit('.', 1)[-1]}#{weapon.Item.data.InstanceId}"
    except Exception:  # noqa: BLE001
        return "none"


# ------------------------------------------------------------------ ring entries

def report_instance(index: int, instance: Any) -> None:
    flags = int(instance.DamageFlags.Flags)
    target = instance.DamagedActor
    say(
        f"{stamp()} ring[{index}] dmg={float(instance.TotalDamage):.1f} flags=0x{flags:04x} "
        f"gunflag={bool(flags & WEAPON_FLAG)} type={described(lambda: instance.DamageType._name)} "
        f"target={described(lambda: target.Name) if target is not None else 'None'} in-hand={weapon_in_hand()}"
    )


def on_tick(obj: UObject, args: WrappedStruct, ret: Any, func: BoundFunction) -> None:
    try:
        pc = get_pc(possibly_loading=True)
        if pc is None:
            return
        ring = getattr(pc, RING_PROPERTY)
        for index in range(RING_SIZE):
            instance = ring[index]
            dirty = int(instance.RepDirtyFlag)
            if dirty == state["dirty"][index]:
                continue
            first_sight = state["dirty"][index] == -1
            state["dirty"][index] = dirty
            if not first_sight:
                report_instance(index, instance)
    except Exception as ex:  # noqa: BLE001
        say(f"tick error: {type(ex).__name__}: {ex}")


# ------------------------------------------------------------------ events

def on_event(obj: UObject, args: WrappedStruct, ret: Any, func: BoundFunction) -> None:
    try:
        say(f"{stamp()} EVENT {func.func.Name} on {described(lambda: obj)} in-hand={weapon_in_hand()}")
        for getter in GETTERS:
            if hasattr(obj, getter):
                say(f"      {getter}() = {described(lambda: getattr(obj, getter)())}")
        for field in STRUCT_FIELDS:
            if hasattr(obj, field):
                say(f"      .{field} = {described(lambda: getattr(obj, field))}")
    except Exception as ex:  # noqa: BLE001
        say(f"event error: {type(ex).__name__}: {ex}")


# ------------------------------------------------------------------ lifecycle

def stop() -> None:
    for path, hook_type in state["hooked"]:
        try:
            if hooks.has_hook(path, hook_type, HOOK_ID):
                hooks.remove_hook(path, hook_type, HOOK_ID)
        except Exception:  # noqa: BLE001
            pass
    state["hooked"].clear()


old_state = getattr(builtins, "_dmgsource_state", None)
if old_state is not None:
    for path, hook_type in old_state.get("hooked", []):
        if hooks.has_hook(path, hook_type, HOOK_ID):
            hooks.remove_hook(path, hook_type, HOOK_ID)
builtins._dmgsource_state = state  # type: ignore[attr-defined]
builtins._dmgsource_stop = stop  # type: ignore[attr-defined]

say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} source probe v2")
state["start"] = time.time()

hooks.add_hook(TICK_FUNC, hooks.Type.POST, HOOK_ID, on_tick)
state["hooked"].append((TICK_FUNC, hooks.Type.POST))
for path in EVENT_FUNCTIONS:
    try:
        unrealsdk.find_object("Function", path)
    except ValueError:
        say(f"   not found: {path}")
        continue
    hooks.add_hook(path, hooks.Type.PRE, HOOK_ID, on_event)
    state["hooked"].append((path, hooks.Type.PRE))
    say(f"   hooked {path}")
say("impact rounds, stickies with a swap before they blow, one grenade, one knife swing; then `pyexec probe_stop.py`")
