"""
Phase 0 probe #3 (v2): prototype the real mechanism.

BL4 applies damage natively - no UFunction fires per hit (see probe_output.txt / calls.tsv).
But OakPlayerController.RecentlyCausedDamageInstances (6-slot ring, RepDirtyFlag++ per write)
holds what the HUD damage numbers show. So: hook a once-per-frame function, watch the dirty
flags, and log each slot that was rewritten.

    pyexec probe_poll.py     start polling (prints each new damage instance to console + file)
    pyexec probe_stop.py     stop
"""

from __future__ import annotations

import builtins
import time
from pathlib import Path
from typing import Any

import unrealsdk
from unrealsdk import hooks, logging

OUT = Path(__file__).parent / "probe_output.txt"
POLL_ID = "dmgpoll"
TICK_FUNC = "/Script/GbxUIUMG.GbxUIUMGTickWidget:BP_TickWidget"  # ~once per frame per the call trace
SLOTS = 6


def say(msg: str) -> None:
    logging.info(f"[dmgpoll] {msg}")
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def err(fn) -> str:
    try:
        return str(fn())
    except Exception as ex:  # noqa: BLE001
        return f"<err {type(ex).__name__}: {ex}>"


# Always start fresh; an older run's state may linger in builtins with a different shape.
old_state = getattr(builtins, "_dmgpoll_state", None)
if old_state is not None and old_state.get("hooked"):
    safe(lambda: hooks.remove_hook(TICK_FUNC, hooks.Type.POST, POLL_ID))
state = {"dirty": {}, "events": 0, "ticks": 0, "hooked": False, "pc": None, "max_per_tick": 0}
builtins._dmgpoll_state = state  # type: ignore[attr-defined]

say("=" * 60)
say(f"{time.strftime('%H:%M:%S')} step 1/5: player controller")
from mods_base import get_pc

pc = get_pc()
state["pc"] = pc
pawn = safe(lambda: pc.Pawn)
say(f"   pc={pc._path_name()}")
say(f"   pawn={err(lambda: pawn._path_name())}")

# ---------------------------------------------------------------- one-off lookups
say("step 2/5: helpers for decoding")

damage_statics = safe(lambda: unrealsdk.find_class("DamageStatics").ClassDefaultObject)
say(f"   DamageStatics CDO: {err(lambda: damage_statics._path_name())}")
for fname in ("Conv_DamageSourceContainer_String", "GetDamageSourceContainerTags", "Conv_DamageSourceContainer_DamageSource"):
    fn = safe(lambda: unrealsdk.find_object("Function", f"/Script/GbxGame.DamageStatics:{fname}"))
    params = err(lambda: ", ".join(f"{p.Name}:{p.Class.Name}" for p in fn._properties()))
    say(f"   {fname}({params})")

say("   enums mentioning Damage (for the Flags bits):")
for en in unrealsdk.find_all("Enum", exact=True):
    n = safe(lambda: str(en.Name), "")
    if "Damage" in n and ("Flag" in n or "Recent" in n or "Number" in n):
        say(f"      {err(lambda: en._path_name())}: {err(lambda: list(en._as_py()) if hasattr(en, '_as_py') else [str(k) for k in en])}")

say("   weapon-ish properties on the pawn (names only):")
names = [str(p.Name) for p in safe(lambda: list(pawn.Class._properties()), [])]
say("      " + ", ".join(n for n in names if "Weapon" in n or "Inventory" in n or "Equipped" in n)[:400])

say("   pawn.ActiveWeapons:")
for i, w in enumerate(safe(lambda: list(pawn.ActiveWeapons), [])):
    say(f"      [{i}] {err(lambda: w._path_name() if w is not None else None)}")
    if w is not None and i == 0:
        wnames = [str(p.Name) for p in safe(lambda: list(w.Class._properties()), [])]
        say(f"          own props: {', '.join(wnames)[:500]}")
        for n in ("Name", "InventoryBalanceData", "BalanceData", "ItemName", "DisplayName", "InventoryData"):
            if n in wnames:
                say(f"          .{n} = {err(lambda: getattr(w, n))[:160]}")
say(f"   pawn.EquippedInventorySlots: {err(lambda: [str(x) for x in pawn.EquippedInventorySlots])[:400]}")


def weapon_now() -> str:
    """Name of the first active weapon on the current pawn - the attribution the mod would use."""
    try:
        pawn = state["pc"].Pawn
        w = pawn.ActiveWeapons[0]
        return str(w.Name) if w is not None else "None"
    except Exception as ex:  # noqa: BLE001
        return f"<err {type(ex).__name__}>"


# What does a DamagedActor expose for health? Names only, cached per class.
health_names: dict[str, list[str]] = {}


def health_of(actor: Any) -> str:
    if actor is None:
        return "target=None"
    cls = actor.Class
    key = str(cls.Name)
    if key not in health_names:
        found = []
        struct = cls
        while struct is not None:
            for p in safe(lambda: list(struct._properties()), []):
                n = str(p.Name)
                if "Health" in n or "DamageState" in n:
                    found.append(n)
            struct = safe(lambda: struct._superfield)
        health_names[key] = found
        say(f"   health-ish props on {key}: {found}")
    parts = []
    for n in ("HealthState", "DamageState"):
        st = safe(lambda: getattr(actor, n))
        if st is None:
            continue
        for p in safe(lambda: list(st._type._properties()), []):
            pn = str(p.Name)
            if "Health" in pn or "Shield" in pn or "Armor" in pn:
                parts.append(f"{n}.{pn}={err(lambda: getattr(st, pn))[:60]}")
    return " ".join(parts)[:500]


def source_str(inst: Any) -> str:
    if damage_statics is None:
        return "?"
    s = err(lambda: damage_statics.Conv_DamageSourceContainer_String(inst.DamageSource))
    tags = err(lambda: damage_statics.GetDamageSourceContainerTags(inst.DamageSource))
    return f"{s} tags={tags[:120]}"


def dtype_str(inst: Any) -> str:
    h = safe(lambda: inst.DamageType)
    if h is None:
        return "?"
    # FGameDataHandle: figure out how to get at the name + resolved data
    name = err(lambda: h.name) if hasattr(h, "name") else err(lambda: h)
    data = safe(lambda: h.data) if hasattr(h, "data") else None
    dot = safe(lambda: data["bIsDoT"]) if data is not None else safe(lambda: h.bIsDoT)
    elem = safe(lambda: data["ElementalType"]) if data is not None else safe(lambda: h.ElementalType)
    return f"{str(name)[:40]} dot={dot} elem={elem}"


FLAG_BITS = {"crit": 1 << 1, "heal": 1 << 2, "shield": 1 << 4, "resist": 1 << 8}  # ERecentPlayerDamageFlags values are bit indexes


def describe(inst: Any) -> str:
    flags = safe(lambda: int(inst.DamageFlags.Flags), 0)
    named = ",".join(n for n, m in FLAG_BITS.items() if flags & m) or "-"
    return (
        f"dmg={safe(lambda: float(inst.TotalDamage), -1.0):.1f} "
        f"flags=0x{flags:x}({named}) "
        f"type[{dtype_str(inst)}] "
        f"target={err(lambda: inst.DamagedActor.Name if inst.DamagedActor is not None else None)} "
        f"src[{source_str(inst)}]"
    )


say("step 3/5: current slots")
say(f"   dir(FGameDataHandle)={err(lambda: [n for n in dir(pc.RecentlyCausedDamageInstances[0].DamageType) if not n.startswith('__')])}")
for i, inst in enumerate(pc.RecentlyCausedDamageInstances):
    state["dirty"][i] = safe(lambda: int(inst.RepDirtyFlag), -1)
    say(f"   [{i}] dirty={state['dirty'][i]} {describe(inst)}")


def on_tick(obj: Any, args: Any, ret: Any, func: Any) -> None:
    state["ticks"] += 1
    try:
        pc = state["pc"]
        arr = pc.RecentlyCausedDamageInstances
        changed = 0
        for i in range(SLOTS):
            inst = arr[i]
            d = int(inst.RepDirtyFlag)
            if d == state["dirty"].get(i):
                continue
            state["dirty"][i] = d
            changed += 1
            state["events"] += 1
            say(f"{time.time():.3f} NEW slot[{i}] dirty={d} weapon={weapon_now()} {describe(inst)} | {health_of(safe(lambda: inst.DamagedActor))}")
        if changed > state["max_per_tick"]:
            state["max_per_tick"] = changed
            if changed >= 5:
                say(f"   !! {changed} slots changed in one frame - ring overflow risk")
    except Exception as ex:  # noqa: BLE001
        say(f"tick error: {type(ex).__name__}: {ex}")


say(f"step 4/5: hooking {TICK_FUNC}")
try:
    unrealsdk.find_object("Function", TICK_FUNC)
except Exception as ex:  # noqa: BLE001
    say(f"   tick function not found: {ex}")
else:
    hooks.add_hook(TICK_FUNC, hooks.Type.POST, POLL_ID, on_tick)
    state["hooked"] = True
    say("step 5/5: polling. Fire single shots, then a SHOTGUN blast, then a grenade. `pyexec probe_stop.py` to stop.")
