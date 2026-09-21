# Nexus page text

Name: BL4 Damage Tracker
Author: DaRealHansForSure
Category: Gameplay / User Interface
Requirements (formal requirement field): Borderlands 4 PythonSDK (oak2-mod-manager, https://github.com/bl-sdk/oak2-mod-manager)

## Description

**See what your guns actually do.**

BL4 Damage Tracker records every damage instance you deal and shows a small table on screen: each equipped weapon with its damage, its DPS while it was in your hands, and its share, plus rows for your action skill, grenades and melee, over the last five minutes of play. The numbers stand still while you are in a menu or a loading screen.

The point of it: compare a gun you just found against the ones you carry. A weapon's DPS is measured only over the time you actually used it in fights, so one minute with a new gun gives a number you can put next to the old ones. The mod also keeps a lifetime record per weapon and per character (damage, DPS in hand, peak DPS, hits, average hit, crit share, kills, time in hand), shown with the `dmgweapons` console command, and writes every session to a log you can report on with `dmgreport`.

**What it tracks**
- bullet impacts, elemental damage, splash, explosive rounds (matched to the gun that fired them, even after a swap), sticky detonations, status ticks (credited to the gun that applied them)
- action skills, grenades, melee, each on its own row
- kills and crits
- the item names of your guns ("Maintained Screeching T.K's Wave"), read from the game's own item cards; the in-game table keeps the short manufacturer-and-type label for width, the dashboard and the records use the item name

**What it does not do**
- track vehicle damage or healing
- work in co-op: untested, single player only

**Console commands** (F6 in the world): `dmgweapons`, `dmgreport`, `dmgreset`, each with `--help`.

## Installation

Requires the Borderlands 4 PythonSDK (oak2-mod-manager). With Vortex: install through the Mod Manager Download button, enable, deploy. Vortex and the game must be on the same drive; the Borderlands 4 Vortex extension does not support symlinks. Manual: extract the zip into the game folder so that `sdk_mods\dmgtracker.sdkmod` exists.

First launch: open the console in the world, type `mods`, enable "BL4 Damage Tracker". It stays enabled.

## Files

Logs and records go to `%LOCALAPPDATA%\BL4DamageTracker\`. The mod writes nothing into the game folder except its settings file under `sdk_mods\settings\`.

## Compatibility

Tested against the game build of 2026-09-19 with oak2-mod-manager v0.3. A game patch can move the internals the mod reads; the mod then logs one clear message and stays idle rather than misbehaving. Source, tests and the notes for fixing it after a patch: (repository link).

## Settings

Four menu entries (logging on/off, table on/off, open log folder, web dashboard on/off). Position, size, window length and the rest are in `sdk_mods\settings\dmgtracker.json`; see the README.

## Changelog

1.2.0: item names of the equipped guns, read from the game's item cards, on the dashboard, in the weapon records and reports (the in-game table keeps the short labels; `readout_item_names` switches it); the dashboard shows the item id next to each gun.
1.1.0: web dashboard at http://127.0.0.1:8765/ (live table over WebSocket, hits with search, weapon Top 10, encounters; also reachable from other devices on the network); Best DPS for Skill and Grenade; weapon records follow the gun across restarts and character switches (records from 1.0.0 are set aside); Melee row hidden by default; table at the left screen border.
1.0.0: first release.
