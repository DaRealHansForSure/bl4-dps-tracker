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
- A gun is recognised across sessions by its item identity (twelve seed bytes of the item, read from the game's item data), so a record follows the gun, not the slot or the session.
- `Best DPS` is the highest value the `DPS` column ever reached for that row, over all sessions of the character: for a gun with at least 10 s in hand inside the window, for Skill and Grenade over at least 10 s of fighting. It only rises; `dmgreset --weapons` clears it.
- A row whose Total grows flashes briefly (in the game and on the dashboard), so you see which source is doing damage right now.
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
| `dmgweb` | Print the dashboard address (`--open` opens it in the browser). |
| `dmgstatus` | Counters, hooks, character, files, item identity, dashboard. |

## Web dashboard

While the game runs, the mod serves a page at `http://127.0.0.1:8765/` (and at your machine's network address, so a phone or a second screen on the same network can show it; `dmgweb` prints both). Four tabs:

- **Live**: the same table as in the game, pushed over a WebSocket whenever it changes, a chart of the window DPS, and the latest hits as they land.
- **Hits**: every hit of a session with search (weapon, target, source, element, character) and pagination; pick any of the kept sessions.
- **Weapons**: the character's lifetime ranking (by DPS in hand, Best DPS or damage) with a Top 10, the full list, and the best DPS of Skill, Grenade and Melee.
- **Encounters**: the fights of a session with duration, total, DPS, crit share, kills, the per-source split and the targets.

The live tab updates itself; the other tabs read the files when you open them or press Reload. "Web dashboard" in the mod menu turns the server on and off; `web_port` and `web_host` in the settings file change the address (`127.0.0.1` keeps it on this machine). Windows may ask once whether the game may accept network connections; that prompt is for the dashboard, and the answer only matters for other devices. The server is read-only and runs on its own threads, never on the game's.

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
| `web_host`, `web_port` | `0.0.0.0`, `8765` | where the dashboard listens (`127.0.0.1` for this machine only) |
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

## How a fight is counted

A fight (encounter) starts with the first damage instance you deal and ends after `encounter_gap_s` seconds (default 15) without any hit; nothing else defines it, not the enemies, not your health. Per fight the mod counts total damage, hits, crits, kills, status damage, the split per source, the targets, and two rates: DPS (total over the fight's length) and active DPS (total over the seconds in which a hit landed). The same rule drives the combat clock behind the table's `Last X min` rows and a gun's time in hand. Details, consequences (two groups within 15 s are one fight, a boss phase longer than the gap splits a fight) and where fights show up: [`docs/encounters.md`](docs/encounters.md).

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
