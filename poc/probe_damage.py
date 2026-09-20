"""
Phase 0 probe: find the function BL4 calls when the player deals damage.

Run from the in-game console (pyexec_root points at this folder):

    pyexec probe_damage.py        enumerate + attach print-hooks
    pyexec probe_stop.py          detach hooks, print per-function call counts

Everything goes to poc/probe_output.txt (append) so the console doesn't flood.
Re-running this file is safe: it removes its own hooks first.
"""

from __future__ import annotations

import builtins
import re
import time
from pathlib import Path
from typing import Any

import unrealsdk
from unrealsdk import hooks, logging
from unrealsdk.unreal import UFunction, UObject, WrappedStruct

OUT = Path(__file__).parent / "probe_output.txt"
HOOK_ID = "dmgprobe"
MAX_PRINTS_PER_FUNC = 40

# Names that smell like "damage is being applied", not "read a damage stat".
INTERESTING = re.compile(
    r"(Receive|Apply|Take|Handle|Inflict|Deal|Process|Notify|On|Report|Register|Cause|K2_|OnRep_)\w*Damage"
    r"|Damage\w*(Received|Applied|Dealt|Taken|Event|Instance|Callback|ConditionMet)",
)
BORING = re.compile(r"^(Get|Can|Should|Is|Has|Calc|Compute|Find|Set)")

# Known-good in BL3 (GbxGameSystemCore); tried first regardless of the regex.
ALWAYS_TRY = (
    "/Script/GbxGameSystemCore.DamageComponent:ReceiveAnyDamage",
    "/Script/GbxGameSystemCore.DamageComponent:ReceiveDamage",
    "/Script/GbxGameSystemCore.DamageComponent:OnDamageReceived",
)


def out(line: str) -> None:
    with OUT.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def fmt(value: Any, depth: int = 0) -> str:
    """Render a hook argument compactly, three levels into structs."""
    try:
        if isinstance(value, UObject):
            return f"<{value.Class.Name} {value._path_name()}>"
        if isinstance(value, WrappedStruct):
            if depth >= 3:
                return f"<struct {value._type.Name}>"
            parts = []
            for prop in value._type._properties():
                name = str(prop.Name)
                try:
                    parts.append(f"{name}={fmt(getattr(value, name), depth + 1)}")
                except Exception as ex:  # noqa: BLE001
                    parts.append(f"{name}=<err {ex}>")
            return f"{value._type.Name}{{" + ", ".join(parts) + "}"
        text = repr(value)
        return text if len(text) < 200 else text[:200] + "..."
    except Exception as ex:  # noqa: BLE001
        return f"<fmt err {ex}>"


def local_player_ids() -> dict[str, str]:
    """Path names of the local PC and pawn so InstigatedBy can be matched by eye."""
    ids: dict[str, str] = {}
    try:
        from mods_base import get_pc

        pc = get_pc()
        ids["pc"] = pc._path_name()
        pawn = getattr(pc, "Pawn", None)
        if pawn is not None:
            ids["pawn"] = pawn._path_name()
    except Exception as ex:  # noqa: BLE001
        ids["error"] = str(ex)
    return ids


# ---------------------------------------------------------------- state

state = getattr(builtins, "_dmgprobe_state", None)
if state is None:
    state = {"hooked": [], "counts": {}, "prints": {}}
    builtins._dmgprobe_state = state  # type: ignore[attr-defined]


def remove_all() -> None:
    for path in state["hooked"]:
        try:
            if hooks.has_hook(path, hooks.Type.POST, HOOK_ID):
                hooks.remove_hook(path, hooks.Type.POST, HOOK_ID)
        except Exception:  # noqa: BLE001
            pass
    state["hooked"].clear()


def on_call(obj: UObject, args: WrappedStruct, ret: Any, func: Any) -> None:
    try:
        path = func.func._path_name()
    except Exception:  # noqa: BLE001
        path = str(func)
    state["counts"][path] = state["counts"].get(path, 0) + 1
    n = state["prints"].get(path, 0)
    if n >= MAX_PRINTS_PER_FUNC:
        return
    state["prints"][path] = n + 1
    try:
        logging.info(f"[dmgprobe] CALL {path.rsplit(':', 1)[-1]} (#{state['counts'][path]})")
        out(f"{time.time():.3f} CALL {path}")
        out(f"    obj={fmt(obj)}")
        out(f"    args={fmt(args)}")
        out(f"    ret={fmt(ret)}")
    except Exception as ex:  # noqa: BLE001
        out(f"    <print err {ex}>")


# ---------------------------------------------------------------- run

remove_all()
out("=" * 78)
say(f"{time.strftime('%H:%M:%S')} step 1/3: listing UFunctions with Damage in the name")
out(f"local={local_player_ids()}")

# 1. Every UFunction in the game whose name contains Damage. Written once, for the notes.
all_damage_funcs: list[str] = []
for fn in unrealsdk.find_all("Function", exact=True):
    try:
        if "Damage" in str(fn.Name):
            all_damage_funcs.append(fn._path_name())
    except Exception:  # noqa: BLE001
        continue
all_damage_funcs.sort()
say(f"step 2/3: {len(all_damage_funcs)} found, picking candidates")
out(f"-- {len(all_damage_funcs)} UFunctions with 'Damage' in the name:")
for p in all_damage_funcs:
    out("   " + p)

# 2. Pick candidates and hook them.
candidates: list[str] = list(ALWAYS_TRY)
for p in all_damage_funcs:
    short = p.rsplit(":", 1)[-1]
    if INTERESTING.search(short) and not BORING.match(short) and p not in candidates:
        candidates.append(p)

say(f"step 3/3: hooking {len(candidates)} candidates")
hooked, missing = [], []
for p in candidates:
    # add_hook happily returns True for paths that don't exist, so check first.
    try:
        unrealsdk.find_object("Function", p)
    except Exception:  # noqa: BLE001
        missing.append(p)
        continue
    try:
        ok = hooks.add_hook(p, hooks.Type.POST, HOOK_ID, on_call)
    except Exception as ex:  # noqa: BLE001
        ok = False
        out(f"   add_hook error {p}: {ex}")
    (hooked if ok else missing).append(p)
state["hooked"] = hooked

out(f"-- hooked {len(hooked)} functions:")
for p in hooked:
    out("   " + p)
out(f"-- {len(missing)} candidates not found: {missing[:10]}{' ...' if len(missing) > 10 else ''}")

say(f"hooked {len(hooked)}, {len(missing)} candidates missing. Fire ONE shot, then `pyexec probe_stop.py`.")
