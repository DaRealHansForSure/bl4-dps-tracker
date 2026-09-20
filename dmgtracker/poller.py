"""
The hot path: once per frame, find out which damage instances are new, and whose they are.

BL4 applies damage natively and no function fires per hit, but the local player controller
keeps a small ring of `RecentPlayerCausedDamageInstance` structs (the source of the HUD's
floating numbers), and every write bumps that slot's RepDirtyFlag. Comparing the six flags
with the previous frame's values catches every instance (see poc/findings.md, section B).

Attribution, all of it from recorded sessions and probes (findings, section B):
  1. healing is not damage and is dropped before it reaches anything;
  2. status ticks (burn, radiation, ...) go to the last hit on that target;
  3. a hit with the gun flag (0x400, bullet impacts and sticky detonations) goes to the gun
     in hand, unless the swap rule applies: within a short grace after a weapon swap, on a
     target the previous gun hit in the last seconds, before the new gun has hit that
     target, the hit is the previous gun's delayed detonation;
  4. an unflagged hit on a target that took a flagged hit in the same frame is the
     elemental part of that bullet: gun in hand;
  5. an unflagged hit within a few frames of a projectile explosion whose owner is a weapon
     (the mod reports those from a hook on projectile:Explode) belongs to that weapon;
     an explosion owned by the character is a grenade;
  6. otherwise the melee window (after a knife swing) or the grenade window (after a throw)
     claims it, the more recent action first;
  7. anything left is Other, held back for a few frames first so a late explosion event can
     still claim it.

The controller is fetched fresh on every tick and never reused across frames: a cached
wrapper points at freed memory once its world is torn down, and reading through it crashed
the game during a character switch (access violation, 2026-09-20).

This module never imports the SDK: the controller is obtained through a callable and only
its attributes are read, so the whole path can be exercised with fakes in the tests.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Any

from .attribution import GRENADE_SOURCE, MELEE_SOURCE, OTHER_SOURCE, SOURCE_WEAPON, DamageSource, WeaponResolver

ERRORS_TO_REMEMBER = 3
# Outside the world (main menu, loading) there is nothing to poll; look again once a second.
IDLE_CHECK_INTERVAL_S = 1.0

# ERecentPlayerDamageFlags values are bit indexes, not masks: CriticalHit=1 -> 0x2, Healing=2 -> 0x4.
# Bits 10 and 14 are not in the reflected enum; their meaning was established from recorded
# sessions: 0x400 is set on bullet impacts and sticky detonations and on nothing else,
# 0x4000 is set on exactly the hit that kills the target.
CRIT_FLAG = 1 << 1
HEALING_FLAG = 1 << 2
WEAPON_FLAG = 1 << 10
KILL_FLAG = 1 << 14

KIND_DIRECT = "direct"
KIND_DOT = "dot"
KIND_HEAL = "heal"

# Status effect ticks carry no reference to what applied them. They are credited to the last
# non-status hit on the same target inside this window; the game's own damage type names end
# in "_Status" for them, and not all of them have bIsDoT set.
STATUS_TYPE_SUFFIX = "_Status"
STATUS_ATTRIBUTION_WINDOW_S = 30.0
MAX_REMEMBERED_TARGETS = 128

# An explosion event and its damage land within about 50 ms of each other, in either order.
EXPLOSION_MATCH_WINDOW_S = 0.25
PENDING_HOLD_S = 0.1
MAX_REMEMBERED_EXPLOSIONS = 32
# Windows after a throw (drone gadgets keep firing for a while), after an explosion owned by
# the character, and after a knife swing (a combo lands up to 3 s after the swing starts).
GRENADE_WINDOW_S = 12.0
EXPLOSION_WINDOW_S = 3.0
MELEE_WINDOW_S = 4.0

# The swap rule: a sticky detonates when its gun is swapped away, and the ring shows the hit
# with the new gun already in hand, within about 0.3 s of the swap being observed. The grace
# stays short so the new gun's own first shot is not taken from it.
SWAP_GRACE_S = 0.75
STICK_WINDOW_S = 10.0

EventSink = Callable[[list[dict[str, Any]]], None]
ControllerResolver = Callable[[], Any]


class DamagePoller:
    """Diffs the controller's damage ring every frame and hands new events to a sink.

    Counters (ticks, events, errors, overflow_frames, ignored_events) are plain attributes
    for the status line. `broken_reason` is set when the ring property is missing, which is
    what a game patch would look like; polling then stops for good instead of failing every
    frame. The note_* methods are fed by the mod's hooks and by its refresh loop.
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
        self.ignored_heals = 0
        self.recent_errors: list[str] = []
        self.broken_reason = ""
        # Called with the new controller when one is adopted, and with None when it is lost.
        self.on_controller_changed: Callable[[Any], None] | None = None
        self._dirty_flags: list[int] = []
        self._controller_address = 0
        self._idle_until = 0.0
        self._last_hit_by_target: dict[str, tuple[DamageSource, float]] = {}
        self._explosions: deque[tuple[float, DamageSource]] = deque()
        self._pending: list[tuple[float, dict[str, Any]]] = []
        self._grenade_until = 0.0
        self._melee_until = 0.0
        self._last_grenade_action = 0.0
        self._last_melee_action = 0.0
        self._current_held: DamageSource | None = None
        self._previous_held: DamageSource | None = None
        self._swapped_at = 0.0
        self._hits_since_swap: set[str] = set()

    # ------------------------------------------------------------------ notes from the mod

    def note_explosion(self, now: float, source: DamageSource) -> None:
        """A projectile owned by `source` (a weapon, or the grenade) just exploded."""
        self._explosions.append((now, source))
        if len(self._explosions) > MAX_REMEMBERED_EXPLOSIONS:
            self._explosions.popleft()
        if source.kind != SOURCE_WEAPON:
            self._grenade_until = max(self._grenade_until, now + EXPLOSION_WINDOW_S)
            self._last_grenade_action = now
        for _deadline, event in self._pending:
            if event["src"] == OTHER_SOURCE.kind and abs(now - float(event["t"])) <= EXPLOSION_MATCH_WINDOW_S:
                self._assign(event, source)

    def note_grenade_thrown(self, now: float) -> None:
        self._grenade_until = max(self._grenade_until, now + GRENADE_WINDOW_S)
        self._last_grenade_action = now

    def note_grenade_exploded(self, now: float) -> None:
        self.note_explosion(now, GRENADE_SOURCE)

    def note_melee_swing(self, now: float) -> None:
        self._melee_until = now + MELEE_WINDOW_S
        self._last_melee_action = now

    def note_weapon_in_hand(self, now: float, held: DamageSource) -> None:
        """Called by the refresh loop; a change of the held weapon starts the swap grace."""
        current = self._current_held
        if current is not None and held.instance_id == current.instance_id:
            return
        if current is not None and current.instance_id != 0:
            self._previous_held = current
            self._swapped_at = now
            self._hits_since_swap = set()
        self._current_held = held

    # ------------------------------------------------------------------ per frame

    def tick(self) -> None:
        """Called once per frame from the tick hook. Never raises."""
        self.ticks += 1
        if self.broken_reason:
            return
        now = self._clock()
        new_events: list[dict[str, Any]] = []
        if now >= self._idle_until:
            try:
                self._refresh_controller()
                if self.controller is None:
                    self._idle_until = now + IDLE_CHECK_INTERVAL_S
                else:
                    new_events = self._collect_new_events(now)
            except Exception as ex:  # noqa: BLE001 - nothing may escape into the game thread
                self._note_error(ex)
                self._drop_controller()
                new_events = []
        ready = self._release_pending(now)
        if new_events or ready:
            batch = sorted(new_events + ready, key=lambda event: float(event["t"]))
            self.events += len(batch)
            self._sink(batch)

    def _release_pending(self, now: float) -> list[dict[str, Any]]:
        if not self._pending:
            return []
        ready = [event for deadline, event in self._pending if deadline <= now]
        if ready:
            self._pending = [(deadline, event) for deadline, event in self._pending if deadline > now]
        return ready

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
        changed: list[Any] = []
        for index in range(self._ring_size):
            instance = ring[index]
            dirty = int(instance.RepDirtyFlag)
            if dirty == self._dirty_flags[index]:
                continue
            self._dirty_flags[index] = dirty
            changed.append(instance)
        if len(changed) == self._ring_size:
            # Every slot changed within one frame: the ring may have wrapped and dropped hits.
            self.overflow_frames += 1
        if not tracking:
            self.ignored_events += len(changed)
            return []
        # Targets that took a bullet impact this frame: their unflagged hits in the same frame
        # are the elemental part of that bullet, not a grenade or a swing.
        flagged_targets: set[str] = set()
        for instance in changed:
            if int(instance.DamageFlags.Flags) & WEAPON_FLAG:
                target = instance.DamagedActor
                if target is not None:
                    flagged_targets.add(str(target.Name))
        ready_now: list[dict[str, Any]] = []
        for instance in changed:
            event = self._read_instance(instance, pawn, now, flagged_targets)
            if event["kind"] == KIND_HEAL:
                self.ignored_heals += 1
                continue
            if self._should_hold(event):
                self._pending.append((now + PENDING_HOLD_S, event))
            else:
                ready_now.append(event)
        return ready_now

    @staticmethod
    def _should_hold(event: dict[str, Any]) -> bool:
        return event["kind"] == KIND_DIRECT and event["src"] == OTHER_SOURCE.kind

    def _read_instance(self, instance: Any, pawn: Any, now: float, flagged_targets: set[str]) -> dict[str, Any]:
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
            source = self._direct_source(flags, target_name, flagged_targets, pawn, now)
            self._remember_hit(target_name, source, now)
        event = {
            "t": now,
            "dmg": float(instance.TotalDamage),
            "kind": kind,
            "crit": bool(flags & CRIT_FLAG),
            "kill": bool(flags & KILL_FLAG),
            "elem": int(getattr(damage_type, "ElementalType", 0) or 0),
            "flags": flags,
            "dtype": type_name,
            "target": target_name,
        }
        self._assign(event, source)
        return event

    @staticmethod
    def _assign(event: dict[str, Any], source: DamageSource) -> None:
        event["src"] = source.kind
        event["weapon"] = source.label
        event["body"] = source.body
        event["wid"] = source.instance_id

    # ------------------------------------------------------------------ attribution

    def _direct_source(self, flags: int, target_name: str, flagged_targets: set[str], pawn: Any, now: float) -> DamageSource:
        if flags & WEAPON_FLAG:
            return self._flagged_source(target_name, pawn, now)
        if target_name in flagged_targets:
            return self._resolver.weapon_in_hand(pawn)
        exploded_by = self._recent_explosion_source(now)
        if exploded_by is not None:
            return exploded_by
        melee_open = now < self._melee_until
        grenade_open = now < self._grenade_until
        if melee_open and (not grenade_open or self._last_melee_action >= self._last_grenade_action):
            return MELEE_SOURCE
        if grenade_open:
            return GRENADE_SOURCE
        return OTHER_SOURCE

    def _flagged_source(self, target_name: str, pawn: Any, now: float) -> DamageSource:
        """The gun in hand, or the previous gun for a sticky that went off on the swap."""
        held = self._resolver.weapon_in_hand(pawn)
        previous = self._previous_held
        within_grace = now - self._swapped_at <= SWAP_GRACE_S
        if previous is not None and within_grace and target_name and target_name not in self._hits_since_swap:
            remembered = self._last_hit_by_target.get(target_name)
            if remembered is not None:
                last_source, when = remembered
                if last_source.instance_id == previous.instance_id and now - when <= STICK_WINDOW_S:
                    return previous
        if target_name:
            self._hits_since_swap.add(target_name)
        return held

    def _recent_explosion_source(self, now: float) -> DamageSource | None:
        for when, source in reversed(self._explosions):
            if abs(now - when) <= EXPLOSION_MATCH_WINDOW_S:
                return source
            if now - when > EXPLOSION_MATCH_WINDOW_S:
                return None
        return None

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
