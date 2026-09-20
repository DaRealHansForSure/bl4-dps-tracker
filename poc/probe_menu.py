"""
Phase 0 probe #12: which flag tells us a menu is open, so the readout can hide?

Collects candidate booleans from the player controller, the HUD, the local player and the
game instance (bool properties and zero-argument bool functions whose name mentions Menu,
Pause, Cursor or Input), plus a few known engine calls, then samples them once a second
for SAMPLE_SECONDS from the per-frame tick hook and prints only the values that changed.

    pyexec probe_menu.py      start sampling, then open/close the ESC menu, the inventory,
                              the map, and stand still in between
    pyexec probe_stop.py      stop early (it also stops by itself after SAMPLE_SECONDS)

Prints to the console and to probe_output.txt.
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
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

OUT = Path(__file__).parent / "probe_output.txt"
HOOK_ID = "dmgmenu"
TICK_FUNC = "/Script/GbxUIUMG.GbxUIUMGTickWidget:BP_TickWidget"
SAMPLE_SECONDS = 120.0
SAMPLE_INTERVAL_S = 0.5
CANDIDATE_NAME_PATTERN = re.compile(
    r"Menu|Pause|Cursor|Input|Modal|Overlay|Focus|Cinematic|HUD|Hidden|Transition|Travel|Loading|Tunnel|Teleport|Streaming|Fade",
    re.IGNORECASE,
)


def say(msg: str) -> None:
    logging.info(f"[dmgmenu] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def described(fn) -> str:
    try:
        value = fn()
    except Exception as ex:  # noqa: BLE001
        return f"<err {type(ex).__name__}: {ex}>"
    if isinstance(value, UObject):
        return f"{value._path_name()} ({value.Class.Name})"
    return repr(value)[:120]


def candidate_readers(label: str, obj: UObject) -> dict[str, Any]:
    """Zero-argument readers for the bool-ish members of obj whose name looks relevant."""
    readers: dict[str, Any] = {}
    seen_functions: set[str] = set()
    for prop in obj.Class._properties():
        prop_name = str(prop.Name)
        if str(prop.Class.Name) != "BoolProperty" or not CANDIDATE_NAME_PATTERN.search(prop_name):
            continue
        readers[f"{label}.{prop_name}"] = (lambda o=obj, n=prop_name: getattr(o, n))
    for field in obj.Class._fields():
        if str(field.Class.Name) != "Function":
            continue
        func_name = str(field.Name)
        if func_name in seen_functions or not CANDIDATE_NAME_PATTERN.search(func_name):
            continue
        seen_functions.add(func_name)
        return_param = field._find_return_param()
        if return_param is None or str(return_param.Class.Name) != "BoolProperty":
            continue
        if field.NumParams != 1:
            continue
        readers[f"{label}.{func_name}()"] = (lambda o=obj, n=func_name: getattr(o, n)())
    return readers


def engine_readers(pc: UObject) -> dict[str, Any]:
    statics = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
    return {
        "GameplayStatics.IsGamePaused": lambda: statics.IsGamePaused(pc),
        "pc.bShowMouseCursor": lambda: pc.bShowMouseCursor,
        "pc.bCinematicMode": lambda: pc.bCinematicMode,
        "pc.IsMoveInputIgnored()": lambda: pc.IsMoveInputIgnored(),
        "pc.IsLookInputIgnored()": lambda: pc.IsLookInputIgnored(),
        "pc.Pawn is None": lambda: pc.Pawn is None,
        "pc.Pawn.bHidden": lambda: pc.Pawn.bHidden,
        "pc.GetViewTarget() is pawn": lambda: pc.GetViewTarget()._path_name() == pc.Pawn._path_name(),
        "hud.bShowHUD": lambda: pc.MyHUD.bShowHUD,
        "hud.bHidden": lambda: pc.MyHUD.bHidden,
        "pc.PlayerCameraManager.bGameCameraCutThisFrame": lambda: pc.PlayerCameraManager.bGameCameraCutThisFrame,
        "pc.Class.Name": lambda: str(pc.Class.Name),
        "world.Name": lambda: str(pc.Pawn.Outer.Name) if pc.Pawn is not None else "no pawn",
    }


state: dict[str, Any] = {"readers": {}, "last": {}, "started": 0.0, "last_sample": 0.0, "hooked": False}


def sample(now: float) -> None:
    changes = []
    for name, reader in state["readers"].items():
        value = described(reader)
        if state["last"].get(name) != value:
            state["last"][name] = value
            changes.append(f"{name} = {value}")
    if changes:
        say(f"{time.strftime('%H:%M:%S')} +{now - state['started']:.0f}s: " + "; ".join(changes))


def on_tick(obj: UObject, args: WrappedStruct, ret: Any, func: BoundFunction) -> None:
    now = time.time()
    if now - state["last_sample"] < SAMPLE_INTERVAL_S:
        return
    state["last_sample"] = now
    try:
        sample(now)
    except Exception as ex:  # noqa: BLE001
        say(f"sample error: {type(ex).__name__}: {ex}")
    if now - state["started"] > SAMPLE_SECONDS:
        stop()
        say("sampling finished")


def stop() -> None:
    if state["hooked"] and hooks.has_hook(TICK_FUNC, hooks.Type.POST, HOOK_ID):
        hooks.remove_hook(TICK_FUNC, hooks.Type.POST, HOOK_ID)
    state["hooked"] = False


old_state = getattr(builtins, "_dmgmenu_state", None)
if old_state is not None and old_state.get("hooked"):
    hooks.remove_hook(TICK_FUNC, hooks.Type.POST, HOOK_ID)
builtins._dmgmenu_state = state  # type: ignore[attr-defined]
builtins._dmgmenu_stop = stop  # type: ignore[attr-defined]

say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} menu probe")
pc = get_pc()
sources = [("pc", pc), ("hud", pc.MyHUD), ("player", pc.Player)]
game_instances = list(unrealsdk.find_all("OakGameInstance"))
if game_instances:
    sources.append(("gi", game_instances[-1]))
for label, obj in sources:
    if obj is None:
        say(f"   {label}: None")
        continue
    readers = candidate_readers(label, obj)
    say(f"   {label} ({obj.Class.Name}): {len(readers)} candidate(s)")
    state["readers"].update(readers)
state["readers"].update(engine_readers(pc))
say(f"   sampling {len(state['readers'])} readers for {SAMPLE_SECONDS:.0f} s; open and close menus now")

state["started"] = time.time()
hooks.add_hook(TICK_FUNC, hooks.Type.POST, HOOK_ID, on_tick)
state["hooked"] = True
sample(state["started"])
