"""
Who is playing: the identity of the current character (save), for per-character stats.

The player state carries `ActiveCharGuid`, a four-int GUID that differs between saves and
stays with a save (probe 2026-09-20, two characters: Char_RoboDealer "Rogue" and
Char_ExoSoldier "Exo-Soldier"). It becomes the folder key under which that character's
records live. This module never imports the SDK; the mod hands it plain values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

CLASS_NAME_PREFIX = "Char_"
UNSIGNED_32_BIT = 0xFFFFFFFF
KEY_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True)
class CharacterIdentity:
    """A character: `key` names the folder, `display` is what the user reads."""

    key: str
    class_name: str
    display: str

    def to_json(self) -> dict[str, str]:
        return {"key": self.key, "class": self.class_name, "name": self.display}


def guid_key(guid_parts: tuple[int, int, int, int]) -> str:
    """32 hex characters from the four signed ints Unreal stores a GUID as."""
    return "".join(f"{part & UNSIGNED_32_BIT:08x}" for part in guid_parts)


def display_name(def_name: str, ui_text: str) -> str:
    """The UI text ("Rogue") when known, else the class name without its Char_ prefix."""
    if ui_text:
        return ui_text
    if def_name.startswith(CLASS_NAME_PREFIX):
        return def_name[len(CLASS_NAME_PREFIX):]
    return def_name or "Unknown"


def identity_from(guid_parts: tuple[int, int, int, int], def_name: str, ui_text: str) -> CharacterIdentity:
    key = guid_key(guid_parts)
    safe_key = KEY_SAFE_PATTERN.sub("", key) or "unknown"
    return CharacterIdentity(safe_key, def_name, display_name(def_name, ui_text))


def read_identity(player_state: Any, pawn: Any) -> CharacterIdentity | None:
    """Build the identity from live objects; None while the state is not ready. Only reads
    attributes, so it works on the SDK wrappers and on plain fakes alike."""
    try:
        guid = player_state.ActiveCharGuid
        guid_parts = (int(guid.A), int(guid.B), int(guid.C), int(guid.D))
        def_name = str(player_state.ReplicatedCharacterDef)
    except Exception:  # noqa: BLE001 - not ready yet, or a different game build
        return None
    if guid_parts == (0, 0, 0, 0):
        return None
    ui_text = ""
    if pawn is not None:
        try:
            ui_text = str(pawn.UXNameRepData.DisplayDef.Text)
        except Exception:  # noqa: BLE001 - the display def is optional
            ui_text = ""
    return identity_from(guid_parts, def_name, ui_text)
