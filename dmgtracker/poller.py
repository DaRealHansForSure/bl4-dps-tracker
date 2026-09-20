"""
The hot path: once per frame, find out which damage instances are new.

BL4 applies damage natively and no function fires per hit, but the local player controller
keeps a small ring of `RecentPlayerCausedDamageInstance` structs (the source of the HUD's
floating numbers), and every write bumps that slot's RepDirtyFlag. Comparing the six flags
with the previous frame's values catches every instance (see poc/findings.md, section B).

This module never imports the SDK: the controller is obtained through a callable and only
its attributes are read, so the whole path can be exercised with fakes in the tests.

The controller is fetched fresh on every tick and never reused across frames: a cached
wrapper points at freed memory once its world is torn down, and reading through it crashed
the game during a character switch (access violation, 2026-09-20).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .attribution import OTHER_SOURCE, DamageSource, WeaponResolver

ERRORS_TO_REMEMBER = 3
# Outside the world (main menu, loading) there is nothing to poll; look again once a second.
IDLE_CHECK_INTERVAL_S = 1.0

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

# Status effect ticks (burn, radiation, ...) carry no reference to what applied them. They are
# credited to the last non-status hit on the same target inside this window; the game's own
# damage type names end in "_Status" for them, and not all of them have bIsDoT set.
STATUS_TYPE_SUFFIX = "_Status"
STATUS_ATTRIBUTION_WINDOW_S = 30.0
MAX_REMEMBERED_TARGETS = 128

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
        self.ignored_events = 0
        self.recent_errors: list[str] = []
        self.broken_reason = ""
        # Called with the new controller when one is adopted, and with None when it is lost.
        self.on_controller_changed: Callable[[Any], None] | None = None
        self._dirty_flags: list[int] = []
        self._controller_address = 0
        self._idle_until = 0.0
        self._last_hit_by_target: dict[str, tuple[DamageSource, float]] = {}

    def tick(self) -> None:
        """Called once per frame from the tick hook. Never raises."""
        self.ticks += 1
        if self.broken_reason:
            return
        now = self._clock()
        if now < self._idle_until:
            return
        try:
            self._refresh_controller()
            if self.controller is None:
                self._idle_until = now + IDLE_CHECK_INTERVAL_S
                return
            new_events = self._collect_new_events(now)
        except Exception as ex:  # noqa: BLE001 - nothing may escape into the game thread
            self._note_error(ex)
            self._drop_controller()
            return
        if new_events:
            self.events += len(new_events)
            self._sink(new_events)

    # ------------------------------------------------------------------ controller

    def _refresh_controller(self) -> None:
        """Take this frame's controller wrapper; adopt it as a new controller when its address
        differs from the last one (map load, character switch), drop it when there is none."""
        controller = self._resolve_controller()
        if controller is None:
            self._drop_controller()
            return
        address = int(controller._get_address())
        if address == self._controller_address:
            self.controller = controller
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
            self._drop_controller()
            return
        # Baseline the flags so instances from before the resolve are not replayed as new hits.
        self._dirty_flags = [int(ring[index].RepDirtyFlag) for index in range(self._ring_size)]
        self.controller = controller
        self._controller_address = address
        if self.on_controller_changed is not None:
            self.on_controller_changed(controller)

    def _drop_controller(self) -> None:
        had_controller = self._controller_address != 0
        self.controller = None
        self._controller_address = 0
        if had_controller and self.on_controller_changed is not None:
            self.on_controller_changed(None)

    # ------------------------------------------------------------------ ring diff

    def _collect_new_events(self, now: float) -> list[dict[str, Any]]:
        ring = getattr(self.controller, self._ring_property)
        pawn = self.controller.Pawn
        # Damage dealt from a vehicle is not the player's gun play; it is dropped, but the
        # flags still advance so those hits are not replayed once the player is on foot.
        tracking = self._resolver.is_character(pawn)
        new_events: list[dict[str, Any]] = []
        changed_slots = 0
        for index in range(self._ring_size):
            instance = ring[index]
            dirty = int(instance.RepDirtyFlag)
            if dirty == self._dirty_flags[index]:
                continue
            self._dirty_flags[index] = dirty
            changed_slots += 1
            if tracking:
                new_events.append(self._read_instance(instance, pawn, now))
            else:
                self.ignored_events += 1
        if changed_slots == self._ring_size:
            # Every slot changed within one frame: the ring may have wrapped and dropped hits.
            self.overflow_frames += 1
        return new_events

    def _read_instance(self, instance: Any, pawn: Any, now: float) -> dict[str, Any]:
        flags = int(instance.DamageFlags.Flags)
        damage_type = instance.DamageType
        target = instance.DamagedActor
        target_name = str(target.Name) if target is not None else ""
        # Some instances (grenades, cleared slots) carry an unresolved damage type handle whose
        # fields raise on access; those hits are still damage, so read the fields with defaults.
        type_name = str(getattr(damage_type, "_name", "None"))
        is_heal = bool(flags & HEALING_FLAG) or bool(getattr(damage_type, "bIsHealingType", False))
        is_status = bool(getattr(damage_type, "bIsDoT", False)) or type_name.endswith(STATUS_TYPE_SUFFIX)
        if is_heal:
            kind = KIND_HEAL
            source = OTHER_SOURCE
        elif is_status:
            kind = KIND_DOT
            source = self._status_source(target_name, now)
        else:
            kind = KIND_DIRECT
            if flags & WEAPON_FLAG:
                source = self._resolver.weapon_in_hand(pawn)
            else:
                source = OTHER_SOURCE
            self._remember_hit(target_name, source, now)
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
            "dtype": type_name,
            "target": target_name,
        }

    # ------------------------------------------------------------------ status attribution

    def _remember_hit(self, target_name: str, source: DamageSource, now: float) -> None:
        if not target_name:
            return
        if len(self._last_hit_by_target) >= MAX_REMEMBERED_TARGETS:
            cutoff = now - STATUS_ATTRIBUTION_WINDOW_S
            stale = [name for name, (_, when) in self._last_hit_by_target.items() if when < cutoff]
            for name in stale:
                del self._last_hit_by_target[name]
            if len(self._last_hit_by_target) >= MAX_REMEMBERED_TARGETS:
                self._last_hit_by_target.clear()
        self._last_hit_by_target[target_name] = (source, now)

    def _status_source(self, target_name: str, now: float) -> DamageSource:
        """The source of the last non-status hit on this target, if recent enough."""
        remembered = self._last_hit_by_target.get(target_name)
        if remembered is None:
            return OTHER_SOURCE
        source, when = remembered
        if now - when > STATUS_ATTRIBUTION_WINDOW_S:
            return OTHER_SOURCE
        return source

    # ------------------------------------------------------------------ errors

    def _note_error(self, ex: Exception) -> None:
        self.errors += 1
        if len(self.recent_errors) < ERRORS_TO_REMEMBER:
            self.recent_errors.append(f"{type(ex).__name__}: {ex}")
