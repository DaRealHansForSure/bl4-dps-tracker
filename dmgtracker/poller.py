"""
The hot path: once per frame, find out which damage instances are new.

BL4 applies damage natively and no function fires per hit, but the local player controller
keeps a small ring of `RecentPlayerCausedDamageInstance` structs (the source of the HUD's
floating numbers), and every write bumps that slot's RepDirtyFlag. Comparing the six flags
with the previous frame's values catches every instance (see poc/findings.md, section B).

This module never imports the SDK: the controller is obtained through a callable and only
its attributes are read, so the whole path can be exercised with fakes in the tests.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .attribution import OTHER_SOURCE, WeaponResolver

CONTROLLER_RETRY_INTERVAL_S = 1.0
ERRORS_TO_REMEMBER = 3

# ERecentPlayerDamageFlags values are bit indexes, not masks: CriticalHit=1 -> 0x2, Healing=2 -> 0x4.
# Bits 10 and 14 are not in the reflected enum; their meaning was established from recorded
# sessions (poc/findings.md, section B): 0x400 is set on every gun hit and on nothing else,
# 0x4000 is set on exactly the hit that kills the target.
CRIT_FLAG = 1 << 1
HEALING_FLAG = 1 << 2
WEAPON_FLAG = 1 << 10
KILL_FLAG = 1 << 14

KIND_DIRECT = "direct"
KIND_DOT = "dot"
KIND_HEAL = "heal"

EventSink = Callable[[list[dict[str, Any]]], None]
ControllerResolver = Callable[[], Any]


class DamagePoller:
    """Diffs the controller's damage ring every frame and hands new events to a sink.

    Counters (ticks, events, errors, overflow_frames) are plain attributes for the status
    line. `broken_reason` is set when the ring property is missing, which is what a game
    patch would look like; polling then stops for good instead of failing every frame.
    """

    def __init__(
        self,
        resolve_controller: ControllerResolver,
        ring_property: str,
        ring_size: int,
        resolver: WeaponResolver,
        sink: EventSink,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._resolve_controller = resolve_controller
        self._ring_property = ring_property
        self._ring_size = ring_size
        self._resolver = resolver
        self._sink = sink
        self._clock = clock
        self.controller: Any = None
        self.ticks = 0
        self.events = 0
        self.errors = 0
        self.overflow_frames = 0
        self.recent_errors: list[str] = []
        self.broken_reason = ""
        self.on_controller_changed: Callable[[Any], None] | None = None
        self._dirty_flags: list[int] = []
        self._last_controller_check = 0.0

    def tick(self) -> None:
        """Called once per frame from the tick hook. Never raises."""
        self.ticks += 1
        if self.broken_reason:
            return
        now = self._clock()
        try:
            if now - self._last_controller_check >= CONTROLLER_RETRY_INTERVAL_S:
                self._last_controller_check = now
                self._refresh_controller()
            if self.controller is None:
                return
            new_events = self._collect_new_events(now)
        except Exception as ex:  # noqa: BLE001 - nothing may escape into the game thread
            self._note_error(ex)
            self.controller = None
            return
        if new_events:
            self.events += len(new_events)
            self._sink(new_events)

    # ------------------------------------------------------------------ controller

    def _refresh_controller(self) -> None:
        """Adopt the current player controller when it appeared or changed (map load, respawn).
        Reading through a stale controller wrapper is not safe, so this runs every second."""
        controller = self._resolve_controller()
        if controller is None:
            self.controller = None
            return
        if self.controller is not None and self._same_object(controller, self.controller):
            return
        ring = getattr(controller, self._ring_property, None)
        if ring is None:
            # The main menu has a controller without a pawn and without the ring; that is not
            # a broken game, just not in the world yet. With a pawn present the ring must exist.
            if controller.Pawn is not None:
                self.broken_reason = (
                    f"property {self._ring_property} is missing on {controller.Class.Name}; "
                    "the game changed, check the ring_property setting"
                )
            return
        # Baseline the flags so instances from before the resolve are not replayed as new hits.
        self._dirty_flags = [int(ring[index].RepDirtyFlag) for index in range(self._ring_size)]
        self.controller = controller
        if self.on_controller_changed is not None:
            self.on_controller_changed(controller)

    @staticmethod
    def _same_object(first: Any, second: Any) -> bool:
        # SDK wrappers are new Python objects on every lookup; the path name identifies the actor.
        return first._path_name() == second._path_name()

    # ------------------------------------------------------------------ ring diff

    def _collect_new_events(self, now: float) -> list[dict[str, Any]]:
        ring = getattr(self.controller, self._ring_property)
        pawn = self.controller.Pawn
        new_events: list[dict[str, Any]] = []
        for index in range(self._ring_size):
            instance = ring[index]
            dirty = int(instance.RepDirtyFlag)
            if dirty == self._dirty_flags[index]:
                continue
            self._dirty_flags[index] = dirty
            new_events.append(self._read_instance(instance, pawn, now))
        if len(new_events) == self._ring_size:
            # Every slot changed within one frame: the ring may have wrapped and dropped hits.
            self.overflow_frames += 1
        return new_events

    def _read_instance(self, instance: Any, pawn: Any, now: float) -> dict[str, Any]:
        flags = int(instance.DamageFlags.Flags)
        damage_type = instance.DamageType
        target = instance.DamagedActor
        if flags & WEAPON_FLAG:
            source = self._resolver.weapon_in_hand(pawn)
        else:
            source = OTHER_SOURCE
        # Some instances (grenades, cleared slots) carry an unresolved damage type handle whose
        # fields raise on access; those hits are still damage, so read the fields with defaults.
        is_heal = bool(flags & HEALING_FLAG) or bool(getattr(damage_type, "bIsHealingType", False))
        if is_heal:
            kind = KIND_HEAL
        elif bool(getattr(damage_type, "bIsDoT", False)):
            kind = KIND_DOT
        else:
            kind = KIND_DIRECT
        return {
            "t": now,
            "dmg": float(instance.TotalDamage),
            "kind": kind,
            "crit": bool(flags & CRIT_FLAG),
            "kill": bool(flags & KILL_FLAG),
            "elem": int(getattr(damage_type, "ElementalType", 0) or 0),
            "flags": flags,
            "src": source.kind,
            "weapon": source.label,
            "body": source.body,
            "wid": source.instance_id,
            "dtype": str(getattr(damage_type, "_name", "None")),
            "target": str(target.Name) if target is not None else "",
        }

    # ------------------------------------------------------------------ errors

    def _note_error(self, ex: Exception) -> None:
        self.errors += 1
        if len(self.recent_errors) < ERRORS_TO_REMEMBER:
            self.recent_errors.append(f"{type(ex).__name__}: {ex}")
