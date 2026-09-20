"""
Throwaway proof of concept for the damage tracker.

Mechanism (found in Phase 0, see poc/findings.md): BL4 applies damage natively, but the
local OakPlayerController keeps a 6-slot ring buffer `RecentlyCausedDamageInstances`
that feeds the HUD's floating numbers. Each write bumps that slot's `RepDirtyFlag`.
We hook a once-per-frame UI function, compare dirty flags, and record every rewritten
slot. A background thread appends the buffer to a JSONL file once a second.

This file exists to answer one question: does polling + buffering + flushing stutter
the game, and does killing the process lose more than a second of data.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import SETTINGS_DIR, build_mod, get_pc, hook
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

TICK_FUNC = "/Script/GbxUIUMG.GbxUIUMGTickWidget:BP_TickWidget"
SLOTS = 6
FLUSH_INTERVAL_S = 1.0
STATUS_EVERY_S = 10.0
CRIT_BIT = 1 << 1  # ERecentPlayerDamageFlags.CriticalHit == 1, values are bit indexes

LOG_PATH = Path(SETTINGS_DIR) / "dmgpoc.jsonl"

# ---------------------------------------------------------------- shared state

_lock = threading.Lock()
_buffer: list[dict[str, Any]] = []
_stop = threading.Event()
_writer: threading.Thread | None = None

_pc: UObject | None = None
_dirty: list[int] = [-1] * SLOTS
_errors = 0
_events_total = 0
_ticks = 0
_last_status = 0.0
_last_pc_retry = 0.0


def _say(msg: str) -> None:
    logging.info(f"[dmgpoc] {msg}")


# ---------------------------------------------------------------- game thread: hot path


def _weapon_name(pawn: Any) -> str:
    """Best effort. ActiveWeapons / EquippedInventorySlots are structs whose layout we
    haven't mapped yet; _dump_weapon_layout() prints it once so the real mod can do better."""
    try:
        aw = pawn.ActiveWeapons
        for prop in aw._type._properties():
            v = getattr(aw, str(prop.Name))
            if isinstance(v, UObject):
                return str(v.Name)
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


def _read_slot(inst: Any, pawn: Any) -> dict[str, Any]:
    flags = int(inst.DamageFlags.Flags)
    dtype = inst.DamageType
    target = inst.DamagedActor
    return {
        "t": time.time(),
        "dmg": float(inst.TotalDamage),
        "weapon": _weapon_name(pawn),
        "kind": "dot" if getattr(dtype, "bIsDoT", False) else "direct",
        "elem": int(getattr(dtype, "ElementalType", 0) or 0),
        "crit": bool(flags & CRIT_BIT),
        "flags": flags,
        "target": str(target.Name) if target is not None else None,
    }


_layout_dumped = False


def _resolve_pc() -> bool:
    global _pc, _dirty, _layout_dumped
    try:
        pc = get_pc()
        arr = pc.RecentlyCausedDamageInstances
        _dirty = [int(arr[i].RepDirtyFlag) for i in range(SLOTS)]
        _pc = pc
    except Exception:  # noqa: BLE001
        _pc = None
        return False
    _say(f"controller resolved: {pc._path_name()}")
    if not _layout_dumped and pc.Pawn is not None:
        _layout_dumped = True
        _dump_weapon_layout(pc.Pawn)
    return True


@hook(TICK_FUNC, Type.POST)
def _on_tick(obj: UObject, args: WrappedStruct, ret: Any, func: BoundFunction) -> None:
    global _pc, _errors, _events_total, _ticks, _last_status, _last_pc_retry
    _ticks += 1
    now = time.time()
    try:
        pc = _pc
        if pc is None:
            # Map change / respawn can invalidate the controller. Retry at most once a second.
            if now - _last_pc_retry > 1.0:
                _last_pc_retry = now
                _resolve_pc()
            return
        arr = pc.RecentlyCausedDamageInstances
        pawn = pc.Pawn
        new = []
        for i in range(SLOTS):
            inst = arr[i]
            d = int(inst.RepDirtyFlag)
            if d == _dirty[i]:
                continue
            _dirty[i] = d
            new.append(_read_slot(inst, pawn))
        if new:
            with _lock:
                _buffer.extend(new)
            _events_total += len(new)
    except Exception as ex:  # noqa: BLE001
        _errors += 1
        if _errors <= 3:
            _say(f"tick error #{_errors}: {type(ex).__name__}: {ex}")
        _pc = None  # force re-resolve
    if now - _last_status > STATUS_EVERY_S:
        _last_status = now
        _say(f"{_events_total} events, {_errors} errors, {_ticks} ticks -> {LOG_PATH.name}")


# ---------------------------------------------------------------- background thread: disk


def _writer_loop() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        while not _stop.wait(FLUSH_INTERVAL_S):
            _flush(f)
        _flush(f)


def _flush(f: Any) -> None:
    global _buffer
    with _lock:
        batch, _buffer = _buffer, []
    if not batch:
        return
    f.write("".join(json.dumps(e, separators=(",", ":")) + "\n" for e in batch))
    f.flush()


# ---------------------------------------------------------------- one-off: map the weapon structs


def _dump_weapon_layout(pawn: Any) -> None:
    for name in ("ActiveWeapons", "EquippedInventorySlots"):
        try:
            st = getattr(pawn, name)
            _say(f"{name}: struct {st._type._path_name()}")
            for prop in st._type._properties():
                pn = str(prop.Name)
                if "Delegate" in str(prop.Class.Name):
                    _say(f"   .{pn}: {prop.Class.Name} (skipped)")
                    continue
                val = getattr(st, pn)
                shown = val._path_name() if isinstance(val, UObject) else repr(val)
                _say(f"   .{pn}: {prop.Class.Name} = {shown[:300]}")
        except Exception as ex:  # noqa: BLE001
            _say(f"{name}: {type(ex).__name__}: {ex}")
    # The equipped weapon objects: which property carries a human-readable name?
    try:
        for i, item in enumerate(pawn.EquippedInventorySlots.items):
            w = item.InstancedInventory
            if w is None:
                continue
            names = [str(p.Name) for p in w.Class._properties()]
            _say(f"slot {i}: {w._path_name()} ({w.Class.Name}); own props: {', '.join(names)[:600]}")
            for n in names:
                if any(k in n for k in ("Name", "Balance", "Def", "Data", "Title")):
                    _say(f"      .{n} = {repr(getattr(w, n))[:160]}")
            if i >= 1:
                break
    except Exception as ex:  # noqa: BLE001
        _say(f"weapon objects: {type(ex).__name__}: {ex}")


# ---------------------------------------------------------------- lifecycle


def on_enable() -> None:
    global _writer, _errors, _events_total, _ticks
    _errors = _events_total = _ticks = 0
    _stop.clear()
    _writer = threading.Thread(target=_writer_loop, name="dmgpoc-writer", daemon=True)
    _writer.start()
    ok = _resolve_pc()
    _say(f"enabled; pc {'ok' if ok else 'not ready, will retry each second'}; log -> {LOG_PATH}")


def on_disable() -> None:
    _stop.set()
    if _writer is not None:
        _writer.join(timeout=3)
    _say(f"disabled; {_events_total} events, {_errors} errors")


build_mod(hooks=[_on_tick], on_enable=on_enable, on_disable=on_disable)
