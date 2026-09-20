"""Weapon labels from body paths, and the resolver against fake game objects."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from dmgtracker.attribution import (
    SOURCE_UNKNOWN,
    SOURCE_WEAPON,
    UNKNOWN_SOURCE,
    WeaponResolver,
    body_stem,
    label_for_body,
)

SNIPER_BODY_PATH = "/Game/Gear/Weapons/Sniper/JAK/Body_JAK_SR.Body_JAK_SR"


def fake_weapon(instance_id: int, body_path: str | None = SNIPER_BODY_PATH, class_name: str = "OakWeapon"):
    """Mimics the attributes the resolver reads off an OakWeapon actor."""
    body = None
    if body_path is not None:
        body = SimpleNamespace(_path_name=lambda: body_path)
    return SimpleNamespace(
        Item=SimpleNamespace(data=SimpleNamespace(InstanceId=instance_id)),
        BodyData=body,
        Class=SimpleNamespace(Name=class_name),
    )


def fake_pawn(held=None, equipped=()):
    slots = [] if held is None else [SimpleNamespace(Weapon=held)]
    items = [SimpleNamespace(InstancedInventory=item) for item in equipped]
    return SimpleNamespace(
        ActiveWeapons=SimpleNamespace(Slots=slots),
        EquippedInventorySlots=SimpleNamespace(items=items),
    )


class BodyStemTests(unittest.TestCase):
    def test_object_name_after_the_dot(self) -> None:
        self.assertEqual(body_stem(SNIPER_BODY_PATH), "Body_JAK_SR")

    def test_path_without_dot_uses_last_segment(self) -> None:
        self.assertEqual(body_stem("/Game/Gear/Body_VLA_AR"), "Body_VLA_AR")

    def test_empty_path(self) -> None:
        self.assertEqual(body_stem(""), "")


class LabelForBodyTests(unittest.TestCase):
    def test_known_codes(self) -> None:
        self.assertEqual(label_for_body("Body_JAK_SR"), "Jakobs Sniper")
        self.assertEqual(label_for_body("Body_VLA_AR"), "Vladof Assault Rifle")
        self.assertEqual(label_for_body("Body_BOR_SM"), "Bor SMG")
        self.assertEqual(label_for_body("Body_TOR_SG"), "Torgue Shotgun")

    def test_unknown_codes_are_kept(self) -> None:
        self.assertEqual(label_for_body("Body_XYZ_QQ"), "XYZ QQ")
        self.assertEqual(label_for_body("Body_XYZ_AR"), "XYZ Assault Rifle")

    def test_unparsable_stem_is_returned_unchanged(self) -> None:
        self.assertEqual(label_for_body("SomethingElse"), "SomethingElse")

    def test_empty_stem_is_unknown(self) -> None:
        self.assertEqual(label_for_body(""), "Unknown")


class WeaponResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = WeaponResolver()

    def test_resolves_the_weapon_in_hand(self) -> None:
        source = self.resolver.weapon_in_hand(fake_pawn(held=fake_weapon(277)))
        self.assertEqual(source.kind, SOURCE_WEAPON)
        self.assertEqual(source.label, "Jakobs Sniper")
        self.assertEqual(source.body, "Body_JAK_SR")
        self.assertEqual(source.instance_id, 277)

    def test_caches_by_instance_id(self) -> None:
        first = self.resolver.resolve_weapon(fake_weapon(277))
        second = self.resolver.resolve_weapon(fake_weapon(277, body_path="/Game/Other/Body_VLA_AR.Body_VLA_AR"))
        self.assertIs(first, second)

    def test_no_pawn_or_empty_slots_is_unknown(self) -> None:
        self.assertIs(self.resolver.weapon_in_hand(None), UNKNOWN_SOURCE)
        self.assertIs(self.resolver.weapon_in_hand(fake_pawn()), UNKNOWN_SOURCE)
        self.assertEqual(self.resolver.weapon_in_hand(fake_pawn()).kind, SOURCE_UNKNOWN)

    def test_slot_without_weapon_is_unknown(self) -> None:
        pawn = SimpleNamespace(ActiveWeapons=SimpleNamespace(Slots=[SimpleNamespace(Weapon=None)]))
        self.assertIs(self.resolver.weapon_in_hand(pawn), UNKNOWN_SOURCE)

    def test_weapon_without_body_data_still_resolves(self) -> None:
        source = self.resolver.resolve_weapon(fake_weapon(5, body_path=None))
        self.assertEqual(source.label, "Unknown")
        self.assertEqual(source.instance_id, 5)

    def test_equipped_weapons_skip_other_gear(self) -> None:
        pawn = fake_pawn(equipped=[
            fake_weapon(1, "/Game/Body_VLA_AR.Body_VLA_AR"),
            fake_weapon(2, "/Game/Body_BOR_SM.Body_BOR_SM"),
            fake_weapon(3, class_name="Shield"),
            None,
        ])
        labels = [source.label for source in self.resolver.equipped_weapons(pawn)]
        self.assertEqual(labels, ["Vladof Assault Rifle", "Bor SMG"])


if __name__ == "__main__":
    unittest.main()
