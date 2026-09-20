# PoC findings

Game: Borderlands 4, Steam, `Borderlands4.exe` dated 2026-09-19 (post Sep-18 patch).
SDK: oak2-mod-manager v0.3 "Whiskey Foxtrot" (pyunrealsdk 1.10.0, unrealsdk 3.2.0, Python 3.14), installed 2026-09-20.

Dev-machine changes outside the repo:
- `OakGame/Binaries/Win64/Plugins/unrealsdk.user.toml` — `pyexec_root` → this `poc/` folder.
- `sdk_mods/__main__.py` line ~470 — `import keybinds` wrapped in `try/except ImportError` (bl-sdk/oak2-mod-manager#17 workaround).

## A. SDK loads on current patch

_pending — launch the game, check `unrealsdk.log`, tilde twice, `mods`, `py print("hi")`_

## B. A hookable function fires per damage instance

_pending — `pyexec probe_damage.py`, one shot, `pyexec probe_stop.py`, read `probe_output.txt`_

Prior art checked:
- BL3 Damage Logger: `/Script/GbxGameSystemCore.DamageComponent:ReceiveAnyDamage` POST, `args.Damage / DamageSource / InstigatedBy / DamageType / Details.bWasCrit`.
- BL4 Damage & More: no damage hook; edits `OakCharacter.DamageState` / `DamageCauserData` on the pawn. Its author is unsure whether BL4 classes live under `/Script/Oak2.` or `/Script/OakGame.` — enumeration needed, not guessing.

## C. Hooked value == on-screen value

_pending_

## D. Buffer + background flush, no stutter

_pending_
