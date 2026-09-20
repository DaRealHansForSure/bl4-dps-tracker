# BL4 Damage Tracker

A Borderlands 4 PythonSDK mod that records every damage instance you deal, shows a per-weapon damage table on screen while you play, and keeps a lifetime record per weapon and per character so you can tell whether a gun you just found is better than the one you carry.

Single player. Tested against the game build of 2026-09-19 with oak2-mod-manager v0.3.

## What you see

```
Weapon                   Total        DPS   Share [%]   Best DPS
Vladof Assault Rifle   442'795     26'047        13.8     31'202      <- the gun in hand is yellow
Jakobs Shotgun         237'166     29'646         7.4     29'646
Vladof SMG             207'749     18'886         6.5     22'410
Torgue Pistol          168'461      7'657         5.2      9'980
Skill                2'034'870     20'349        63.2     24'118
Grenade                120'322      1'204         3.7      3'350
Other                    7'703         77         0.2

Last 5 min           3'219'066     32'190       100.0
```

- Every number covers the **last 5 minutes of fighting**. The clock only runs while a fight is on, so menus, driving, loading and walking between fights do not move the numbers.
- A weapon row's **DPS is damage per second while that gun was in your hands during fights**. A gun you used for one minute out of five gets its own fair number; that is the column to compare a new gun against an old one.
- `Total` is the window damage, `Share [%]` the share of the window total.
- `Best DPS` is the highest value the `DPS` column ever reached for that row, over all sessions of the character: for a gun with at least 10 s in hand inside the window, for Skill and Grenade over at least 10 s of fighting. It only rises; `dmgreset --weapons` clears it.
- Rows never move: four weapon slots in slot order, then Skill, Grenade, Melee and Other, then the total. Melee is hidden by default (`readout_hidden_rows`); a hidden row's damage still counts in the total. A gun swapped out of the slots drops from the table; its damage stays in the total and in its lifetime record.
- The table hides in menus, in vehicles and during tunnel transitions. Vehicle damage is not tracked. Healing is never counted.
- Numbers use Swiss formatting: `'` for thousands, `.` for decimals.

## Console commands

Open the console with F6 (in the world, not in the main menu):

| Command | What it does |
|---|---|
| `dmgweapons` | Rank the current character's weapons over all sessions: DPS in hand, peak DPS, damage, hits, average hit, crit share, kills, minutes in hand. `-n 30` for a longer list. |
| `dmgreport` | Report over the current session: each fight (total, duration, DPS, active DPS, targets, kills, crit share, per-source split), a session summary, a weapons table. `--last 300` adds the average over the final five minutes, `--gap 30` changes the fight boundary. |
| `dmgreset` | Reset the on-screen table. `--weapons` also starts a fresh lifetime record for the current character, `--all` for every character; old records are kept as `weapons.reset-<timestamp>.json`. |

## Files

Everything the mod writes goes to `%LOCALAPPDATA%\BL4DamageTracker\` (`C:\Users\<you>\AppData\Local\BL4DamageTracker\`):

- `logs\session-<date>-<time>.jsonl`: one file per game launch, one JSON line per hit, plus a meta line and a marker line whenever you switch character. The newest 20 sessions are kept. `latest-session.txt` names the running one.
- `characters\<character id>\weapons.json`: that character's lifetime weapon record.

Use the "Open log folder" button in the mod menu to get there.

## Settings

The mod menu (`mods` in the console) has three entries: `Logging enabled`, `Show damage table`, `Open log folder`. Everything else lives in `sdk_mods\settings\dmgtracker.json`, editable by hand while the game is closed:

| Setting | Default | Meaning |
|---|---|---|
| `log_dir` | `%LOCALAPPDATA%\BL4DamageTracker\logs` | where sessions are written; environment variables allowed |
| `readout_window_minutes` | `5.0` | the table's window, in minutes of fighting |
| `readout_show_encounter_row` | `false` | add an `Encounter` row (the current fight, for bosses) |
| `readout_x`, `readout_y` | `32`, `320` | position of the table (top left corner, in pixels) |
| `readout_show_best_column` | `true` | the `Best DPS` column |
| `readout_hidden_rows` | `["melee"]` | non-weapon rows to leave out: `melee`, `grenade`, `skill`, `other` |
| `readout_font_size` | `30` | font size |
| `readout_column_gap`, `readout_column_widths` | `44`, `[320, 240, 190, 140]` | column layout |
| `readout_background_alpha` | `0.6` | background darkness |
| `encounter_gap_s` | `15.0` | seconds without a hit that end a fight |
| `sessions_to_keep` | `20` | session files kept |
| `flush_interval_s` | `1.0` | how often the log reaches the disk |
| `tick_function`, `ring_property`, `ring_size` | game internals | only relevant after a game patch, see `docs/hook-notes.md` |

## Install

**Vortex** (needs ChemBoy1's Borderlands 4 extension, and the PythonSDK installed): install the release zip through Vortex, enable, deploy. Vortex and the game must be on the same drive; the extension does not support symlinks.

**Manual**: extract the release zip into the game folder (`...\Steam\steamapps\common\Borderlands 4\`), which puts `sdk_mods\dmgtracker.sdkmod` in place.

First launch: open the console in the world, type `mods`, and enable "BL4 Damage Tracker". It stays enabled afterwards.

## How it works, in one paragraph

BL4 applies damage natively; no Unreal function fires per hit. The local player controller keeps a six-slot ring of the damage instances it caused (the source of the floating damage numbers), so the mod hooks a per-frame UI function, diffs the ring's dirty flags, and records every new instance. Bullet impacts carry a gun flag and go to the gun in hand; explosive rounds are matched to their gun through the projectile that exploded; status ticks go to the last hit on that target; grenades, knife swings and action skills are recognised through the game's own throw, trick and skill events; sticky rounds that detonate on a weapon swap go to the previous gun. Details, evidence and dead ends are in [`docs/hook-notes.md`](docs/hook-notes.md).

## Known limitations

- Weapon names are manufacturer plus type ("Jakobs Sniper #277"); the game exposes no item display name at runtime. The number after `#` is the item's id, stable across sessions, so two Jakobs snipers stay apart.
- Damage that lands in the same frame as a knife swing during a running action skill cannot be told from the swing and counts as melee.
- A very fast burst of more than six instances within one frame could overflow the game's ring; the log window reports such frames.
- Untested in co-op.

## Development

```
C:\Development\Python311\python.exe -m unittest discover -s tests -t .
C:\Development\Python311\python.exe build.py
C:\Development\Python311\python.exe dmgtracker\report.py [session.jsonl] [--last 300]
```

Stdlib only (the SDK embeds Python 3.14, the dev machine tests on 3.11). `dmgtracker/` is the mod; `tests/` covers every module that does not touch the SDK; `poc/` holds the probes that established the mechanism, with `poc/findings.md` as the record. To develop against the game, link `sdk_mods\dmgtracker` to the repo's `dmgtracker` folder with a directory junction and restart the game after each change.
