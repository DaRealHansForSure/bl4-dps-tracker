# BL4 Damage Tracker: full-build plan

Written 2026-09-20 after the proof of concept closed (all five checks in `poc/findings.md` pass). This is the plan for the real mod; the PoC code in `poc/` stays as reference and is not reused.

## What v1 delivers

1. An SDK mod `dmgtracker` that records every damage instance the player deals to a JSONL file and shows three live numbers on screen.
2. `report.py`, an offline report over a recorded session (per-encounter totals, averages, per-weapon split, rolling `--last N`), also runnable from the in-game console as `dmgreport`.
3. `build.py`, producing the `.sdkmod` and the Nexus zip.
4. Tests, a README, hook notes and the Nexus page text.

Not in v1: grenade and skill attribution (data is logged so it can be added), item display names such as "Lionized Gruppagrak" (separate project, the strings live in the game's localization tables), the local web dashboard (see the last section; v1 prepares for it), damage taken, co-op.

## Decisions carried into the build

- Mechanism: per-frame hook on `/Script/GbxUIUMG.GbxUIUMGTickWidget:BP_TickWidget` (POST), diffing `RepDirtyFlag` across the six `OakPlayerController.RecentlyCausedDamageInstances` slots. Nothing else fires per hit in BL4.
- Weapon attribution: gun hits carry flag `0x400`; those go to the weapon in hand at the moment of the hit, `pc.Pawn.ActiveWeapons.Slots[0].Weapon`, labelled from the `BodyData` path stem (`Body_JAK_SR` -> "Jakobs Sniper") and identified by `Item.data.InstanceId`. Hits without that flag (grenades, melee, skills) are source `other`, shown as one "Other" row, never pinned on a gun.
- On-screen readout: a runtime UMG `UserWidget` with `TextBlock` children, added to the viewport above the Coherent HUD (findings section E).
- Number formatting everywhere the user reads a number: Swiss style, `'` thousands separator, `.` decimal point (`1'234'567.5`). One shared helper, no ad-hoc `format(x, ",")`.
- Configuration through mods_base options persisted in `sdk_mods/settings/dmgtracker.json` (a `config.json` inside the `.sdkmod` zip would not be editable). Mechanism knobs and layout knobs such as the readout position are `HiddenOption`s in that file, which is also where future settings go; the file is hand-editable and documented in the README.
- Logs under `%LOCALAPPDATA%\BL4DamageTracker\logs\session-<YYYYMMDD-HHMMSS>.jsonl` (user data stays out of the game folder under Program Files), newest 20 kept; the folder is the `log_dir` setting, environment variables allowed.
- Release: one zip `dmgtracker-<version>.zip` containing only `sdk_mods/dmgtracker.sdkmod`. Vortex strips the leading folder and drops the `.sdkmod` into `sdk_mods`; manual installers extract to the game root. No loose files next to the `.sdkmod` (the loader logs an error for any file in `sdk_mods` that is not a valid `.sdkmod`).
- Stdlib only, Python 3.14 syntax level (the SDK's embedded interpreter); the dev machine runs tests with `C:\Development\Python311\python.exe`, so no 3.12+ syntax in code that tests import.
- Code style: four-space indentation, braces on every block, plain hyphens instead of em dashes.

## Event record

One JSON object per line, raw values, no formatting:

```
{"t": 1789924156.446, "dmg": 48.04, "kind": "direct", "crit": true, "elem": 0, "flags": 17410,
 "src": "weapon", "weapon": "Jakobs Sniper", "body": "Body_JAK_SR", "wid": 277,
 "dtype": "DT_Bullet_JAK", "target": "Char_CatYoung_2147476409"}
```

| Field | Source | Notes |
|---|---|---|
| `t` | `time.time()` at the tick that saw the slot change | wall clock, never frame counts |
| `dmg` | `TotalDamage` | as the HUD shows it (PoC check C) |
| `kind` | `DamageType.bIsDoT`, healing flag | `direct`, `dot`, `heal`; the report ignores `heal` |
| `crit` | `DamageFlags.Flags & 0x2` | |
| `kill` | `DamageFlags.Flags & 0x4000` | the hit that killed the target (decoded from recorded sessions) |
| `elem` | `DamageType.ElementalType` | int, mapping documented later |
| `flags` | `DamageFlags.Flags` | raw, for later decoding |
| `src` | attribution | `weapon` when `Flags & 0x400` (gun damage), else `other` (grenade, melee, skill); `unknown` when no weapon could be read |
| `weapon` | attribution label | "Manufacturer Type" from the body path, "Other" for non-gun damage |
| `body` | `BodyData` path stem | keeps the raw code for a future name lookup |
| `wid` | `Item.data.InstanceId` | keeps two guns of the same body apart |
| `dtype` | `repr(DamageType)` name | the hook for classifying grenade and skill damage later |
| `target` | `DamagedActor.Name` | raw actor name, the report strips the numeric suffix |

The first line of every file is `{"meta": {"mod": "dmgtracker", "version": "1.0.0", "schema": 1, "started": <t>}}`. Readers skip lines that carry `meta`; `schema` lets a future consumer refuse or adapt to a changed record layout. The log folder also holds `latest-session.txt` with the file name of the running session, rewritten at enable, so a tailer never has to guess by modification time.

## Mod layout

```
dmgtracker/
  __init__.py       build_mod, options, enable/disable, self-check, status line, dmgreport command
  poller.py         hot path: the tick hook, ring diff, one dict per changed slot
  attribution.py    DamageSource dataclass, resolve_source(), body path parsing, label cache
  logwriter.py      buffer + lock, flush thread, session file, meta line, pruning
  readout.py        the UMG widget: create, position, update text, remove
  stats.py          the one stats engine: EncounterTracker and RollingWindow, fed event by event
  formatting.py     format_number() in Swiss style, used by readout and report
  report.py         offline report over a session file; replays it through stats.py
  pyproject.toml    name, version, author, description, [tool.sdkmod]
```

Rules that shape the modules:

- `poller.py` is the only file that runs every frame. Per frame it reads six `RepDirtyFlag`s; only on a change does it read the slot and call attribution. Everything inside a broad `try/except` that counts errors, logs the first three, and forces a controller re-resolve (at most once per second). No file IO, no formatting.
- `attribution.py` has two layers: pure functions (`parse_body_path`, `label_for_body`) with no SDK imports, testable on the dev machine; and `resolve_source(pawn, instance)` that touches SDK objects and caches the label per `InstanceId`. Adding grenade or skill attribution later means adding a resolver branch keyed on `dtype`, not touching the poller.
- `logwriter.py` swaps the buffer under the lock and appends the batch in a daemon thread every `flush_interval` seconds; the session file opens at enable; pruning to 20 sessions runs at enable. After each written batch it calls every registered flush listener with that batch (none in v1); that is the seam for an in-process publisher later, without touching the poller.
- `readout.py` owns the widget lifecycle. The widget is created when the controller resolves and recreated if the controller object changes (map load); `on_disable` removes it. Text updates happen on the game thread from the tick hook, at most every 250 ms and only when a value changed.
- `stats.py` is pure Python (no SDK), updated per event in O(1): `EncounterTracker` opens an encounter on the first hit and closes it after the 5 s gap (yielding the closed encounter with total, duration, per-source split, targets, crit count), `RollingWindow` keeps the last 60 s in a deque. The readout feeds it live, `report.py` replays a file through it, and the future dashboard tails a file into it, so all three agree by construction.
- `formatting.py`: `format_number(value, decimals=0) -> str`, e.g. `1'234'567` and `51'687.5`.
- Self-check on enable: `find_object("Function", tick_function)` inside `try`, and the ring property on the controller once resolved. On failure: one `logging.error` naming the expected path and the settings file, hook not installed, mod stays enabled but idle. No per-frame exception spam after a game patch.
- A status line to the SDK log every 60 s: events, errors, ticks, overflow-suspect frames (all six slots changed in one frame, which means the ring may have dropped hits), buffer size.

## In-game readout

A table, always showing every equipped weapon (the four weapon slots, in slot order, even at zero), rendered as approved with `poc/probe_table.py`. Every number covers the last X minutes (default 5, setting `readout_window_minutes`): in the open world that is what a player wants to see; encounters are for boss fights and appear as an optional extra row (off by default).

```
Weapon            Total        DPS   Share [%]
Vladof AR     1'234'567     51'687        62.1      <- weapon in hand, yellow
Bor SMG         456'789     19'032        23.0
Jakobs Sniper   296'010     12'334        14.9
Torgue Shotgun        0          0         0.0
Other            12'400        413         0.6

Last 5 min    1'999'766      6'666       100.0      <- light blue
Encounter       987'366     82'807       100.0      <- optional, off by default
```

- Rows: header (grey), one row per equipped weapon slot (labels refreshed once a second from `EquippedInventorySlots`, damage keyed by `wid`), then weapons used in the window but no longer equipped, an `Other` row (grenade, melee, skills) and a `DoT` row when they have damage, a spacer, the `Last X min` row, and the `Encounter` row when enabled.
- The purpose is comparing guns, so a weapon row's `DPS` is damage per second **while that weapon was in hand during a fight** (time in hand is sampled every 250 ms while an encounter is open and the table is visible; carrying a gun around between fights does not count, and a gun that was used for one minute out of five gets its own fair number). `Total` is the window damage, `Share [%]` the share of the window total. The `Last X min` row's DPS is total over the window (or over the time since the first hit while the session is younger than the window).
- Hidden while `pc.bShowMouseCursor` is set (ESC menu, inventory, map) or there is no pawn; shown again on the same frame the menu closes.
- Implementation per findings section E: `WidgetBlueprintLibrary.Create(pc, GbxUIUMGUserWidget, pc)`, a `Border` root with a `GridPanel`, one `TextBlock` per cell, styling through `SetFont`, `SetJustification`, `SetMinDesiredWidth`, `SetColorAndOpacity`, `SetShadowOffset`, `SetShadowColorAndOpacity`, `AddToViewport(10000)`, `SetPositionInViewport`. Cells are created once (a fixed capacity of rows, unused rows collapsed) and updated with `SetText` only when their value changed, at most every 250 ms, on the game thread from the tick hook.
- Recreated when the controller object changes (map load); removed on disable and when the readout option is turned off.

## Lifetime weapon record

`%LOCALAPPDATA%\BL4DamageTracker\weapons.json` keeps, per weapon instance id (stable across game restarts, verified over three restarts): label, body, damage, hits, crits, kills, seconds in hand, first and last seen. Updated from every hit and every held-time sample, saved by the log writer thread every 30 s when changed and at disable (atomic replace; a corrupt file is set aside, not overwritten). The console command `dmgweapons [-n N]` prints the ranking by DPS in hand with damage, hits, average hit, crit share, kills and minutes in hand. This is the "which gun performs best" answer across sessions; the report gets the same section.

## Options

Visible in the mod menu:

- `Logging enabled` (bool, default on)
- `Show readout` (bool, default on)
- `Open log folder` (button, `os.startfile`)

Hidden (persisted, editable in `sdk_mods/settings/dmgtracker.json`):

- `log_dir` (default `%LOCALAPPDATA%\BL4DamageTracker\logs`)
- `tick_function` (default `/Script/GbxUIUMG.GbxUIUMGTickWidget:BP_TickWidget`)
- `ring_property` (default `RecentlyCausedDamageInstances`), `ring_size` (6)
- `flush_interval_s` (1.0), `sessions_to_keep` (20), `encounter_gap_s` (15.0; the spec's 5 s ended fights during weapon swaps and grenade throws in the first real sessions; also written to the session meta line so the report uses the same gap)
- `readout_x`, `readout_y` (120, 320), `readout_font_size` (30), `readout_column_gap` (44), `readout_column_widths` ([320, 240, 190, 140]), `readout_background_alpha` (0.6), `readout_window_minutes` (5.0), `readout_show_encounter_row` (false): the readout layout and scope, changed by editing the settings file (no menu entry; the file is reloaded at enable)

## report.py

Stdlib, `python report.py [session.jsonl] [--last N] [--gap S]`, run from the repo checkout or an unzipped `.sdkmod` (it imports `stats.py` and `formatting.py` from its own folder). With no file argument it picks the session named in `latest-session.txt`, falling back to the newest file in the default log folder.

Per encounter (5 s gap constant at the top of the file): total, duration, honest avg/s, active avg/s (seconds with at least one hit), top targets by hit count (so the boss fight is recognisable), per-weapon split as absolute and percent with DoT held out on its own line and counted in the total, crit share as one number. Session summary: total, encounter count, average encounter, largest encounter. `--last N` averages raw damage over the final N seconds regardless of encounters. All numbers through `format_number`. Text to stdout; no HTML.

The mod registers a console command `dmgreport [--last N]` that runs the same functions on the current session file and prints to the SDK console, so the colleague never needs a Python install; that is the primary way to read a report.

## Tests

`tests/` with `unittest`, run as `C:\Development\Python311\python.exe -m unittest discover -s tests`:

- `test_formatting.py`: Swiss format cases (0, 999, 1000, 1'234'567.5, negatives, rounding).
- `test_attribution.py`: body path parsing for the known codes and an unknown code (falls back to the raw code).
- `test_stats.py`: encounter open/close on the gap, per-source split, rolling window pruning, averages.
- `test_report.py`: against `tests/fixtures/sample-session.jsonl` with known totals, `--last`, `heal` exclusion, DoT held out of the split, meta line skipped.

The fixture is a real session recorded with the finished mod (replacing a synthetic one used during development), with its expected totals written next to it.

## Build and release

`build.py` (stdlib): reads the version from `dmgtracker/pyproject.toml`, zips `dmgtracker/` (excluding `__pycache__`, tests, and anything not needed at runtime) as `build/dmgtracker.sdkmod` with the folder as the single root entry (the loader requires exactly that), then wraps it as `build/dmgtracker-<version>.zip` containing `sdk_mods/dmgtracker.sdkmod`. Prints both paths and sizes.

End-to-end test before any upload: install the zip through Vortex on this machine (mod removed first), enable in the `mods` menu, launch, fight, confirm the session file and the readout.

## Docs

- `README.md`: what it does, install (Vortex and manual), the readout, running the report (`dmgreport` in game, `report.py` outside), where the logs are, known limitations (weapon labels are manufacturer + type, ring overflow in extreme burst, single player), development setup.
- `docs/hook-notes.md`: the mechanism and everything tried, distilled from `poc/findings.md`, for whoever fixes the next patch breakage.
- `docs/nexus-page.md`: page text with the SDK requirement, the same-drive Vortex note, single player only, tested game build date.

## Order of work and gates

1. Mod core: `poller`, `attribution`, `logwriter`, `formatting`, options, self-check, status line. Deploy to `sdk_mods/`, one 2-minute session. Gate: plausible line count, `weapon` labels correct for the guns used, 0 errors in the status lines, no stutter.
2. Readout: `readout`, `stats`. Session with a map change or respawn. Gate: the table shows every equipped weapon, updates during a fight, hides in menus, survives the controller change; the menu toggle hides and shows it.
3. Report: `report.py`, `dmgreport`, tests, real fixture. Gate: report totals equal the readout's last encounter, `--last 300` sane, all tests green.
4. Packaging: `build.py`, Vortex end-to-end install. Gate: fresh install works from the zip alone.
5. Docs and the Nexus page text. Then tag `v1.0.0`.

One commit per step, messages in the existing style.

## Open points

Decided on 2026-09-20: display name "BL4 Damage Tracker", author "DaRealHansForSure"; readout position defaults to (120, 320) and is changed through the settings file; `dmgreport` prints to the SDK console only.

## Future extension: local web dashboard

Wanted later: a web page served locally that shows every collected stat and updates live over a WebSocket stream. Design intent so v1 does not stand in its way:

- The dashboard is a separate process (`webstats/`, not part of the `.sdkmod`), which tails the session file named in `latest-session.txt` and pushes each new event line to the browser. Live latency is the flush interval (1 s). The game process never hosts a web server, so a dashboard bug can never stall the game thread.
- It reuses `stats.py` and `formatting.py` for encounters, averages and number formatting, so the page, the readout and the report show the same numbers.
- Transport: a WebSocket server is about 150 lines of stdlib (`hashlib` and `base64` for the handshake, `struct` for frames); Server-Sent Events is the simpler stdlib alternative if bidirectional traffic is never needed. Decide then. The external process is free to use third-party packages, unlike the mod.
- If sub-second latency is ever wanted, the flush-listener seam in `logwriter.py` allows an in-process publisher (for example a UDP broadcast of each batch) without changing the poller.
- History across sessions comes from the same log folder (20 sessions kept by default; raise `sessions_to_keep` for a longer history).
