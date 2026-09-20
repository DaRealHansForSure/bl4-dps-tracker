"""The ring-buffer poller against a fake player controller."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from dmgtracker.attribution import WeaponResolver
from dmgtracker.poller import CRIT_FLAG, HEALING_FLAG, KILL_FLAG, WEAPON_FLAG, DamagePoller

RING_PROPERTY = "RecentlyCausedDamageInstances"
RING_SIZE = 6


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def fake_instance(damage: float = 0.0, flags: int = 0, is_dot: bool = False, is_heal_type: bool = False,
                  target: str | None = "Char_CatAdult_1", dtype: str = "Normal") -> SimpleNamespace:
    damaged_actor = None if target is None else SimpleNamespace(Name=target)
    return SimpleNamespace(
        RepDirtyFlag=0,
        TotalDamage=damage,
        DamageFlags=SimpleNamespace(Flags=flags),
        DamageType=SimpleNamespace(bIsDoT=is_dot, bIsHealingType=is_heal_type, ElementalType=0, _name=dtype),
        DamagedActor=damaged_actor,
    )


def fake_weapon(instance_id: int, body_path: str) -> SimpleNamespace:
    return SimpleNamespace(
        Item=SimpleNamespace(data=SimpleNamespace(InstanceId=instance_id)),
        BodyData=SimpleNamespace(_path_name=lambda: body_path),
        Class=SimpleNamespace(Name="OakWeapon"),
    )


def fake_controller(name: str = "OakPlayerController_1", with_ring: bool = True, with_pawn: bool = True,
                    address: int = 0x1000):
    weapon = fake_weapon(277, "/Game/Body_JAK_SR.Body_JAK_SR")
    pawn = None
    if with_pawn:
        pawn = SimpleNamespace(
            Class=SimpleNamespace(Name="OakCharacter"),
            ActiveWeapons=SimpleNamespace(Slots=[SimpleNamespace(Weapon=weapon)]),
            EquippedInventorySlots=SimpleNamespace(items=[]),
        )
    controller = SimpleNamespace(
        Pawn=pawn,
        Class=SimpleNamespace(Name="OakPlayerController"),
        _path_name=lambda: f"/Game/Maps/World_P:PersistentLevel.{name}",
        _get_address=lambda: address,
    )
    if with_ring:
        setattr(controller, RING_PROPERTY, [fake_instance() for _ in range(RING_SIZE)])
    return controller


def write_hit(controller, slot: int, damage: float, flags: int = 0, **kwargs) -> None:
    """Simulate the game writing a damage instance into one ring slot."""
    ring = getattr(controller, RING_PROPERTY)
    previous_dirty = ring[slot].RepDirtyFlag
    ring[slot] = fake_instance(damage, flags, **kwargs)
    ring[slot].RepDirtyFlag = (previous_dirty + 1) % 256


class DamagePollerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.controller = fake_controller()
        self.current_controller = self.controller
        self.received: list[dict] = []
        self.poller = DamagePoller(
            resolve_controller=lambda: self.current_controller,
            ring_property=RING_PROPERTY,
            ring_size=RING_SIZE,
            resolver=WeaponResolver(),
            sink=self.received.extend,
            clock=self.clock,
        )

    def test_first_tick_adopts_the_controller_without_replaying_old_hits(self) -> None:
        write_hit(self.controller, 0, 100.0)
        self.poller.tick()
        self.assertIs(self.poller.controller, self.controller)
        self.assertEqual(self.received, [])

    def test_new_hits_become_events(self) -> None:
        self.poller.tick()
        write_hit(self.controller, 1, 48.04, flags=CRIT_FLAG | WEAPON_FLAG, target="Char_CatYoung_9", dtype="Corrosive")
        self.poller.tick()
        self.assertEqual(len(self.received), 1)
        event = self.received[0]
        self.assertEqual(event["dmg"], 48.04)
        self.assertTrue(event["crit"])
        self.assertFalse(event["kill"])
        self.assertEqual(event["kind"], "direct")
        self.assertEqual(event["src"], "weapon")
        self.assertEqual(event["weapon"], "Jakobs Sniper")
        self.assertEqual(event["wid"], 277)
        self.assertEqual(event["target"], "Char_CatYoung_9")
        self.assertEqual(event["dtype"], "Corrosive")
        self.assertEqual(event["t"], self.clock.now)
        self.assertEqual(self.poller.events, 1)

    def test_hits_without_the_weapon_flag_are_other_damage(self) -> None:
        self.poller.tick()
        write_hit(self.controller, 0, 333.0, flags=KILL_FLAG)
        write_hit(self.controller, 1, 333.0, flags=0)
        self.poller.tick()
        first, second = self.received
        self.assertEqual((first["src"], first["weapon"], first["wid"]), ("other", "Other", 0))
        self.assertTrue(first["kill"])
        self.assertEqual(second["src"], "other")
        self.assertFalse(second["kill"])

    def test_vehicle_hits_are_ignored_but_not_replayed(self) -> None:
        self.poller.tick()
        self.controller.Pawn = SimpleNamespace(Class=SimpleNamespace(Name="OakVehicle"))
        write_hit(self.controller, 0, 500.0, flags=WEAPON_FLAG)
        self.poller.tick()
        self.assertEqual(self.received, [])
        self.assertEqual(self.poller.ignored_events, 1)
        self.controller.Pawn = fake_controller().Pawn
        self.poller.tick()
        self.assertEqual(self.received, [], "the vehicle hit must not surface once on foot")
        write_hit(self.controller, 1, 20.0, flags=WEAPON_FLAG)
        self.poller.tick()
        self.assertEqual([event["dmg"] for event in self.received], [20.0])

    def test_unchanged_slots_produce_nothing(self) -> None:
        self.poller.tick()
        write_hit(self.controller, 2, 10.0)
        self.poller.tick()
        self.poller.tick()
        self.poller.tick()
        self.assertEqual(len(self.received), 1)

    def test_dot_and_heal_kinds(self) -> None:
        self.poller.tick()
        write_hit(self.controller, 0, 5.0, flags=WEAPON_FLAG, is_dot=True)
        write_hit(self.controller, 1, 30.0, flags=WEAPON_FLAG | HEALING_FLAG)
        write_hit(self.controller, 2, 30.0, flags=WEAPON_FLAG, is_heal_type=True)
        self.poller.tick()
        self.assertEqual([event["kind"] for event in self.received], ["dot", "heal", "heal"])

    def test_status_ticks_are_credited_to_the_last_hit_on_that_target(self) -> None:
        self.poller.tick()
        write_hit(self.controller, 0, 50.0, flags=WEAPON_FLAG, target="Char_Leader_7", dtype="Radiation")
        write_hit(self.controller, 1, 300.0, flags=0, target="Char_Psycho_3", dtype="Normal")
        self.poller.tick()
        write_hit(self.controller, 2, 6.3, flags=0x90, target="Char_Leader_7", dtype="Radiation_Status")
        write_hit(self.controller, 3, 6.3, flags=0x80, target="Char_Psycho_3", dtype="Radiation_Status", is_dot=True)
        write_hit(self.controller, 4, 6.3, flags=0x80, target="Char_Unknown_1", dtype="Radiation_Status")
        self.poller.tick()
        ticks = self.received[2:]
        self.assertEqual([event["kind"] for event in ticks], ["dot", "dot", "dot"])
        self.assertEqual([event["weapon"] for event in ticks], ["Jakobs Sniper", "Other", "Other"])
        self.assertEqual([event["src"] for event in ticks], ["weapon", "other", "other"])

    def test_status_attribution_expires(self) -> None:
        self.poller.tick()
        write_hit(self.controller, 0, 50.0, flags=WEAPON_FLAG, target="Char_Leader_7")
        self.poller.tick()
        self.clock.advance(31.0)
        write_hit(self.controller, 1, 6.3, flags=0x80, target="Char_Leader_7", dtype="Radiation_Status")
        self.poller.tick()
        self.assertEqual(self.received[-1]["weapon"], "Other")

    def test_unresolved_damage_type_handle_is_tolerated(self) -> None:
        self.poller.tick()
        write_hit(self.controller, 0, 250.0, flags=WEAPON_FLAG)
        # An unresolved FGameDataHandle raises AttributeError for every field except _name.
        getattr(self.controller, RING_PROPERTY)[0].DamageType = SimpleNamespace(_name="None")
        self.poller.tick()
        self.assertEqual(self.poller.errors, 0)
        event = self.received[0]
        self.assertEqual(event["dmg"], 250.0)
        self.assertEqual(event["kind"], "direct")
        self.assertEqual(event["elem"], 0)
        self.assertEqual(event["dtype"], "None")

    def test_target_gone_is_empty_string(self) -> None:
        self.poller.tick()
        write_hit(self.controller, 0, 5.0, target=None)
        self.poller.tick()
        self.assertEqual(self.received[0]["target"], "")

    def test_all_slots_changed_counts_as_overflow_suspect(self) -> None:
        self.poller.tick()
        for slot in range(RING_SIZE):
            write_hit(self.controller, slot, 1.0)
        self.poller.tick()
        self.assertEqual(self.poller.overflow_frames, 1)
        self.assertEqual(len(self.received), RING_SIZE)

    def test_read_error_is_counted_and_controller_dropped(self) -> None:
        self.poller.tick()
        getattr(self.controller, RING_PROPERTY)[0] = SimpleNamespace(RepDirtyFlag="not a number")
        self.poller.tick()
        self.assertEqual(self.poller.errors, 1)
        self.assertIsNone(self.poller.controller)
        self.assertEqual(len(self.poller.recent_errors), 1)

    def test_controller_change_is_adopted_at_once(self) -> None:
        self.poller.tick()
        replacement = fake_controller(name="OakPlayerController_2", address=0x2000)
        write_hit(replacement, 0, 7.0)
        self.current_controller = replacement
        self.poller.tick()
        self.assertIs(self.poller.controller, replacement)
        # The hit from before the adoption is baseline, not replayed.
        self.assertEqual(self.received, [])
        write_hit(replacement, 0, 8.0, flags=WEAPON_FLAG)
        self.poller.tick()
        self.assertEqual([event["dmg"] for event in self.received], [8.0])
        self.assertEqual(self.received[0]["weapon"], "Jakobs Sniper")

    def test_controller_change_callback_reports_adoption_and_loss(self) -> None:
        seen = []
        self.poller.on_controller_changed = seen.append
        self.poller.tick()
        self.poller.tick()
        self.assertEqual(seen, [self.controller])
        self.current_controller = None
        self.poller.tick()
        self.poller.tick()
        self.assertEqual(seen, [self.controller, None])
        self.current_controller = self.controller
        self.clock.advance(1.0)
        self.poller.tick()
        self.assertEqual(seen, [self.controller, None, self.controller])

    def test_same_controller_is_not_readopted_across_frames(self) -> None:
        self.poller.tick()
        write_hit(self.controller, 0, 3.0, flags=WEAPON_FLAG)
        # A fresh wrapper with the same address is the same controller; hits must not be lost.
        self.current_controller = fake_controller()
        setattr(self.current_controller, RING_PROPERTY, getattr(self.controller, RING_PROPERTY))
        self.poller.tick()
        self.assertEqual([event["dmg"] for event in self.received], [3.0])

    def test_no_controller_is_quiet_and_backs_off(self) -> None:
        calls = []
        self.current_controller = None
        self.poller._resolve_controller = lambda: calls.append(1)  # noqa: SLF001 - counting lookups
        self.poller.tick()
        self.poller.tick()
        self.poller.tick()
        self.assertEqual(len(calls), 1, "only one lookup per idle second")
        self.clock.advance(1.0)
        self.poller.tick()
        self.assertEqual(len(calls), 2)
        self.assertIsNone(self.poller.controller)
        self.assertEqual(self.poller.errors, 0)

    def test_menu_controller_backs_off_too(self) -> None:
        self.current_controller = fake_controller(with_ring=False, with_pawn=False)
        self.poller.tick()
        first_ticks = self.poller.ticks
        self.poller.tick()
        self.assertEqual(self.poller.ticks, first_ticks + 1)
        self.assertIsNone(self.poller.controller)

    def test_menu_controller_without_ring_is_not_broken(self) -> None:
        self.current_controller = fake_controller(with_ring=False, with_pawn=False)
        self.poller.tick()
        self.assertEqual(self.poller.broken_reason, "")
        self.assertIsNone(self.poller.controller)

    def test_ring_missing_in_the_world_marks_the_mechanism_broken(self) -> None:
        self.current_controller = fake_controller(with_ring=False, with_pawn=True)
        self.poller.tick()
        self.assertIn(RING_PROPERTY, self.poller.broken_reason)
        self.clock.advance(5.0)
        self.poller.tick()
        self.assertEqual(self.poller.errors, 0)


if __name__ == "__main__":
    unittest.main()
