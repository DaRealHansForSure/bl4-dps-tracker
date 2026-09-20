"""
Phase 0 probe #13: is there a real link from a damage instance to the weapon that caused it?

Bullet impacts carry flag 0x400 and are credited to the gun in hand; explosions, splash and
elemental damage arrive unflagged, and a projectile can go off after a weapon swap. Two
places may hold the link: the ring entry's DamageSource container (empty in the one check
made so far), and the projectile objects whose lifecycle functions fire around detonations.

    pyexec probe_source.py    start: prints every new ring entry with the decoded
                              DamageSource container, and every projectile event with the
                              object's owner/instigator and weapon-ish fields
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
MAX_VALUE_CHARS = 220
WEAPON_FLAG = 1 << 10
EVENT_FUNCTIONS = (
    "/Script/GbxGame.LightProjectileScript:OnBegin",
    "/Script/GbxGame.LightProjectileScript:OnImpact",
    "/Script/GbxGame.LightProjectileScript:OnLifetimeExpired",
    "/Script/GbxGame.projectile:Explode",
    "/Script/OakGame.GrenadeGadget:ExplodeCallback",
    "/Script/OakGame.OakCharacter:ThrowGadget",
    "/Script/GbxGame.LightProjectileStatics:SpawnLightProjectileAsync",
)
INTERESTING_FRAGMENTS = ("Weapon", "Source", "Inventory", "Spawner", "Causer", "Owner", "Instigator", "Item", "Gadget")
MAX_FIELDS_PER_OBJECT = 20


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


state: dict[str, Any] = {"dirty": [-1] * RING_SIZE, "hooked": [], "statics": None, "dumped_classes": set(), "start": 0.0}


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

def describe_damage_source(container: Any) -> str:
    statics = state["statics"]
    as_string = described(lambda: statics.Conv_DamageSourceContainer_String(container))
    tags = described(lambda: statics.GetDamageSourceContainerTags(container))
    as_source = described(lambda: statics.Conv_DamageSourceContainer_DamageSource(container))
    return f"str={as_string} tags={tags} src={as_source}"


def report_instance(index: int, instance: Any) -> None:
    flags = int(instance.DamageFlags.Flags)
    target = instance.DamagedActor
    say(
        f"{stamp()} ring[{index}] dmg={float(instance.TotalDamage):.1f} flags=0x{flags:04x} "
        f"gunflag={bool(flags & WEAPON_FLAG)} type={described(lambda: instance.DamageType._name)} "
        f"target={described(lambda: target.Name) if target is not None else 'None'} in-hand={weapon_in_hand()}"
    )
    say(f"      source: {describe_damage_source(instance.DamageSource)}")
    say(f"      raw: {described(lambda: repr(instance))}")


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


# ------------------------------------------------------------------ projectile events

def interesting_fields(obj: UObject) -> list[str]:
    lines = []
    for prop in obj.Class._properties():
        name = str(prop.Name)
        if not any(fragment in name for fragment in INTERESTING_FRAGMENTS):
            continue
        if "Delegate" in str(prop.Class.Name):
            continue
        lines.append(f"{name}={described(lambda: getattr(obj, name))}")
        if len(lines) >= MAX_FIELDS_PER_OBJECT:
            break
    return lines


def on_event(obj: UObject, args: WrappedStruct, ret: Any, func: BoundFunction) -> None:
    try:
        say(f"{stamp()} EVENT {func.func.Name} on {described(lambda: obj)} in-hand={weapon_in_hand()}")
        class_name = str(obj.Class.Name)
        if class_name not in state["dumped_classes"]:
            state["dumped_classes"].add(class_name)
            names = [str(p.Name) for p in obj.Class._properties()]
            say(f"      {class_name} props: {', '.join(names)[:1500]}")
        say(f"      fields: {'; '.join(interesting_fields(obj))[:1500]}")
        arg_lines = []
        for prop in args._type._properties():
            name = str(prop.Name)
            arg_lines.append(f"{name}={described(lambda: getattr(args, name))}")
        if arg_lines:
            say(f"      args: {'; '.join(arg_lines)[:1200]}")
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
say(f"{time.strftime('%H:%M:%S')} source probe")
state["start"] = time.time()
state["statics"] = unrealsdk.find_class("DamageStatics").ClassDefaultObject

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
say("fire impact rounds, then stickies with a swap before they blow, a grenade, a knife swing; then `pyexec probe_stop.py`")
