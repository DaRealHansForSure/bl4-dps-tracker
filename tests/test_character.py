"""Character identity from the player state."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from dmgtracker.character import display_name, guid_key, identity_from, read_identity

ROGUE_GUID = (-2109537248, 1240118128, 649679508, -1966960911)
EXO_GUID = (1426336294, 1167264140, -52377165, -349395467)


def fake_state(guid: tuple[int, int, int, int], def_name: str = "Char_RoboDealer") -> SimpleNamespace:
    return SimpleNamespace(
        ActiveCharGuid=SimpleNamespace(A=guid[0], B=guid[1], C=guid[2], D=guid[3]),
        ReplicatedCharacterDef=def_name,
    )


def fake_pawn(ui_text: str) -> SimpleNamespace:
    return SimpleNamespace(UXNameRepData=SimpleNamespace(DisplayDef=SimpleNamespace(Text=ui_text)))


class GuidKeyTests(unittest.TestCase):
    def test_signed_parts_become_32_hex_characters(self) -> None:
        key = guid_key(ROGUE_GUID)
        self.assertEqual(len(key), 32)
        self.assertRegex(key, r"^[0-9a-f]{32}$")
        self.assertNotEqual(key, guid_key(EXO_GUID))

    def test_key_is_stable(self) -> None:
        self.assertEqual(guid_key(ROGUE_GUID), guid_key(ROGUE_GUID))


class DisplayNameTests(unittest.TestCase):
    def test_ui_text_wins(self) -> None:
        self.assertEqual(display_name("Char_RoboDealer", "Rogue"), "Rogue")

    def test_falls_back_to_the_class_without_prefix(self) -> None:
        self.assertEqual(display_name("Char_ExoSoldier", ""), "ExoSoldier")
        self.assertEqual(display_name("", ""), "Unknown")


class ReadIdentityTests(unittest.TestCase):
    def test_reads_state_and_pawn(self) -> None:
        identity = read_identity(fake_state(ROGUE_GUID), fake_pawn("Rogue"))
        self.assertEqual(identity.key, guid_key(ROGUE_GUID))
        self.assertEqual(identity.class_name, "Char_RoboDealer")
        self.assertEqual(identity.display, "Rogue")
        self.assertEqual(identity.to_json(), {"key": identity.key, "class": "Char_RoboDealer", "name": "Rogue"})

    def test_missing_pawn_or_display_def_still_gives_an_identity(self) -> None:
        identity = read_identity(fake_state(EXO_GUID, "Char_ExoSoldier"), None)
        self.assertEqual(identity.display, "ExoSoldier")
        broken_pawn = SimpleNamespace(UXNameRepData=None)
        self.assertEqual(read_identity(fake_state(EXO_GUID, "Char_ExoSoldier"), broken_pawn).display, "ExoSoldier")

    def test_not_ready_gives_none(self) -> None:
        self.assertIsNone(read_identity(None, None))
        self.assertIsNone(read_identity(SimpleNamespace(), fake_pawn("Rogue")))
        self.assertIsNone(read_identity(fake_state((0, 0, 0, 0)), fake_pawn("Rogue")))

    def test_identity_from_matches_read(self) -> None:
        self.assertEqual(identity_from(ROGUE_GUID, "Char_RoboDealer", "Rogue"),
                         read_identity(fake_state(ROGUE_GUID), fake_pawn("Rogue")))


if __name__ == "__main__":
    unittest.main()
