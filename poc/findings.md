# PoC findings

Game: Borderlands 4, Steam, `Borderlands4.exe` dated 2026-09-19 (post Sep-18 patch).
SDK: oak2-mod-manager v0.3 "Whiskey Foxtrot" (pyunrealsdk 1.10.0, unrealsdk 3.2.0, Python 3.14), installed 2026-09-20.

Dev-machine changes outside the repo:
- `OakGame/Binaries/Win64/Plugins/unrealsdk.user.toml`: `console_key = "F6"`, `external_console = true`, `pyexec_root` -> this `poc/` folder.
- `sdk_mods/__main__.py` line ~470: `import keybinds` wrapped in `try/except ImportError`. Turned out unnecessary on this build (keybinds loaded fine, see A), harmless to keep.
- `sdk_mods/dmgpoc/`: copy of `poc/dmgpoc/`, enabled once via the `mods` menu (remembered in `sdk_mods/settings/dmgpoc.json`).

## A. SDK loads on current patch: PASS

- unrealsdk, pyunrealsdk, console_mod_menu and keybinds all initialise on the Sep-19 build. Issue #17 did not reproduce here.
- Tilde did nothing on this machine (German layout, and BL4 binds Tilde itself). BL4-specific SDK code overwrites the game's console key when `unrealsdk.console_key` is set, so F6 works. `Input.ini` in the user config folder is ignored by the game.
- The console only opens in the world, not on the main menu.

## B. A hookable function fires per damage instance: PASS, with a different mechanism than planned

- 170 UFunctions with `Damage` in the name exist; 62 plausible ones were hooked. None fired for a gunshot. `log_all_calls` over one shot shows zero weapon/hit/damage functions. BL4 applies damage natively; there is no per-hit function to hook.
- `add_hook` returns True for paths that do not exist. Validate with `unrealsdk.find_object("Function", path)` first.
- What works: `OakPlayerController.RecentlyCausedDamageInstances`, a fixed array of 6 `RecentPlayerCausedDamageInstance` structs (the source of the HUD floating numbers). Written round-robin; each write increments that slot's `RepDirtyFlag` (byte). Diffing dirty flags once per frame from a hook on `/Script/GbxUIUMG.GbxUIUMGTickWidget:BP_TickWidget` (~60-100 calls/s) catches every instance. Keying on anything else (target, location) produces false events when a dead target's weak pointer goes null.
- Struct fields: `TotalDamage` (float), `DamageType` (FGameDataHandle with `bIsDoT`, `ElementalType`, `DamageIconFrameName`, name via repr), `DamagedActor` (weak Actor), `DamageLocation`, `DamageFlags.Flags` (uint32), `DamageSource` (DamageSourceContainer, empty in this replicated copy: `Conv_DamageSourceContainer_String` returns ''), `RepDirtyFlag`.
- `ERecentPlayerDamageFlags` values are bit indexes, not masks: CriticalHit=1 -> `Flags & 0x2`, Healing=2 -> `0x4`, Shield=4 -> `0x10`, Resist=8 -> `0x100`. Gun hits also carry `0x400` and `0x4000` (meaning unknown, `0x4000` absent on some hits).
- Weapon attribution: `DamageSource` is empty, so use the pawn's equipped weapon. `pawn.EquippedInventorySlots.items[i].InstancedInventory` is an `OakWeapon`; `pawn.ActiveWeapons` is an `OakActiveWeaponsState` struct (contains a delegate pyunrealsdk cannot read; skip delegate props when dumping). Which property carries the display name is still open; the dmgpoc mod prints the OakWeapon property names on first resolve.
- Target health for the overkill note: `DamagedActor.HealthState.HealthTypeStates[]` (per health type: flesh/armor/shield) and `DamageState`. Not yet mapped.
- The pawn object changes on respawn; the controller object persisted across the session but may change on map load. Re-resolve `get_pc()` on any read error.
- Ring overflow: >6 instances in one frame would drop events. Untested (no shotgun available). The HUD has the same limit, so the game probably merges pellets, but verify.

## C. Hooked value == on-screen value: PASS

Three controlled comparisons: 394 shown / 393.6 read, 367 / 367.4, 366 / 366.3. Crit flag matched the visual crit each time. One earlier report of 46 shown vs 48.0 read could not be reproduced; treat as unconfirmed.

## D. Buffer + background flush, no stutter: PENDING

`poc/dmgpoc/` implements the mechanism: per-frame diff -> list append -> daemon thread appends JSONL every second to `sdk_mods/settings/dmgpoc.jsonl`. Test: 2 minute firefight, kill the process from Task Manager, check stutter and how much data the last second lost. First attempt failed on an `UnboundLocalError` (fixed, redeployed, not yet re-run).
