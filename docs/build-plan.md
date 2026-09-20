# BL4 Damage Tracker: full-build plan

Written 2026-09-20 after the proof of concept closed (all five checks in `poc/findings.md` pass). This is the plan for the real mod; the PoC code in `poc/` stays as reference and is not reused.

## What v1 delivers

1. An SDK mod `dmgtracker` that records every damage instance the player deals to a JSONL file and shows three live numbers on screen.
2. `report.py`, an offline report over a recorded session (per-encounter totals, averages, per-weapon split, rolling `--last N`), also runnable from the in-game console as `dmgreport`.
3. `build.py`, producing the `.sdkmod` and the Nexus zip.
4. Tests, a README, hook notes and the Nexus page text.

Not in v1: grenade and skill attribution (data is logged so it can be added), item display names such as "Lionized Gruppagrak" (separate project, the strings live in the game's localization tables), damage taken, co-op.

## Decisions carried into the build

- Mechanism: per-frame hook on `/Script/GbxUIUMG.GbxUIUMGTickWidget:BP_TickWidget` (POST), diffing `RepDirtyFlag` across the six `OakPlayerController.RecentlyCausedDamageInstances` slots. Nothing else fires per hit in BL4.
- Weapon attribution: the weapon in hand at the moment of the hit, `pc.Pawn.ActiveWeapons.Slots[0].Weapon`. Label from the `BodyData` path stem (`Body_JAK_SR` -> "Jakobs Sniper"), identity from `Item.data.InstanceId`.
- On-screen readout: a runtime UMG `UserWidget` with `TextBlock` children, added to the viewport above the Coherent HUD (findings section E).
- Number formatting everywhere the user reads a number: Swiss style, `'` thousands separator, `.` decimal point (`1'234'567.5`). One shared helper, no ad-hoc `format(x, ",")`.
- Configuration through mods_base options persisted in `sdk_mods/settings/dmgtracker.json` (a `config.json` inside the `.sdkmod` zip would not be editable). Mechanism knobs are `HiddenOption`s.
- Logs under `sdk_mods/settings/dmgtracker/logs/session-<YYYYMMDD-HHMMSS>.jsonl`, newest 20 kept.
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
| `elem` | `DamageType.ElementalType` | int, mapping documented later |
| `flags` | `DamageFlags.Flags` | raw, for later decoding |
| `src` | attribution | `weapon` in v1; `grenade`, `skill`, `unknown` reserved |
| `weapon` | attribution label | "Manufacturer Type" from the body path |
| `body` | `BodyData` path stem | keeps the raw code for a future name lookup |
| `wid` | `Item.data.InstanceId` | keeps two guns of the same body apart |
| `dtype` | `repr(DamageType)` name | the hook for classifying grenade and skill damage later |
| `target` | `DamagedActor.Name` | raw actor name, the report strips the numeric suffix |

The first line of every file is `{"meta": {"mod": "dmgtracker", "version": "1.0.0", "started": <t>}}`. Readers skip lines that carry `meta`.

## Mod layout

```
dmgtracker/
  __init__.py       build_mod, options, enable/disable, self-check, status line, dmgreport command
  poller.py         hot path: the tick hook, ring diff, one dict per changed slot
  attribution.py    DamageSource dataclass, resolve_source(), body path parsing, label cache
  logwriter.py      buffer + lock, flush thread, session file, meta line, pruning
  readout.py        the UMG widget: create, position, update text, remove
  live_stats.py     running encounter total, honest average, rolling 60 s window
  formatting.py     format_number() in Swiss style, used by readout and report
  report.py         offline report; stdlib only, importable by the mod and runnable standalone
  pyproject.toml    name, version, author, description, [tool.sdkmod]
```

Rules that shape the modules:

- `poller.py` is the only file that runs every frame. Per frame it reads six `RepDirtyFlag`s; only on a change does it read the slot and call attribution. Everything inside a broad `try/except` that counts errors, logs the first three, and forces a controller re-resolve (at most once per second). No file IO, no formatting.
- `attribution.py` has two layers: pure functions (`parse_body_path`, `label_for_body`) with no SDK imports, testable on the dev machine; and `resolve_source(pawn, instance)` that touches SDK objects and caches the label per `InstanceId`. Adding grenade or skill attribution later means adding a resolver branch keyed on `dtype`, not touching the poller.
- `logwriter.py` swaps the buffer under the lock and appends the batch in a daemon thread every `flush_interval` seconds; the session file opens at enable; pruning to 20 sessions runs at enable.
- `readout.py` owns the widget lifecycle. The widget is created when the controller resolves and recreated if the controller object changes (map load); `on_disable` removes it. Text updates happen on the game thread from the tick hook, at most every 250 ms and only when a value changed.
- `live_stats.py` is pure Python (no SDK), updated per event in O(1): current encounter total and duration (5 s gap rule), honest average per second, rolling 60 s sum via a deque. The encounter gap constant is shared with `report.py` by import; a test asserts the two agree.
- `formatting.py`: `format_number(value, decimals=0) -> str`, e.g. `1'234'567` and `51'687.5`.
- Self-check on enable: `find_object("Function", tick_function)` inside `try`, and the ring property on the controller once resolved. On failure: one `logging.error` naming the expected path and the settings file, hook not installed, mod stays enabled but idle. No per-frame exception spam after a game patch.
- A status line to the SDK log every 60 s: events, errors, ticks, overflow-suspect frames (all six slots changed in one frame, which means the ring may have dropped hits), buffer size.

## In-game readout

Three lines, monospace-ish, top left under the HUD by default, position adjustable:

```
Encounter   2'481'003
Avg/s          51'687
Last 60 s      44'120
```

- Encounter total resets when the 5 s gap rule closes the encounter; the closed value stays on screen until the next hit.
- Avg/s is total divided by wall-clock duration of the encounter (honest, counts reloads).
- Last 60 s is the raw sum of the rolling window divided by 60.
- Implementation per findings section E: `WidgetBlueprintLibrary.Create(pc, container_class, pc)`, a `VerticalBox` root with three `TextBlock`s (fallback: one `TextBlock` with newlines if the box does not lay out), `AddToViewport(10000)`, `SetPositionInViewport`. Container class tried in order: `GbxUIUMGUserWidget`, then `GbxUIUMGListViewItemWidget` (the one that worked). Font size and colour through `SetFont`/`SetColorAndOpacity` if they take structs cleanly; otherwise the default 24 pt font stays.

## Options

Visible in the mod menu:

- `Logging enabled` (bool, default on)
- `Show readout` (bool, default on)
- `Open log folder` (button, `os.startfile`)

Hidden (persisted, editable in `sdk_mods/settings/dmgtracker.json`):

- `tick_function` (default `/Script/GbxUIUMG.GbxUIUMGTickWidget:BP_TickWidget`)
- `ring_property` (default `RecentlyCausedDamageInstances`), `ring_size` (6)
- `flush_interval_s` (1.0), `sessions_to_keep` (20)
- `readout_x`, `readout_y` (120, 320), `readout_font_size` (24)

## report.py

Standalone, stdlib, `python report.py [session.jsonl] [--last N] [--gap S]`. With no file argument it picks the newest session in the default log folder.

Per encounter (5 s gap constant at the top of the file): total, duration, honest avg/s, active avg/s (seconds with at least one hit), top targets by hit count (so the boss fight is recognisable), per-weapon split as absolute and percent with DoT held out on its own line and counted in the total, crit share as one number. Session summary: total, encounter count, average encounter, largest encounter. `--last N` averages raw damage over the final N seconds regardless of encounters. All numbers through `format_number`. Text to stdout; no HTML.

The mod registers a console command `dmgreport [--last N]` that runs the same functions on the current session file and prints to the SDK console, so the colleague never needs a Python install. The mod also copies `report.py` into the log folder at enable (overwriting when the version line differs) for offline use.

## Tests

`tests/` with `unittest`, run as `C:\Development\Python311\python.exe -m unittest discover -s tests`:

- `test_formatting.py`: Swiss format cases (0, 999, 1000, 1'234'567.5, negatives, rounding).
- `test_attribution.py`: body path parsing for the known codes and an unknown code (falls back to the raw code).
- `test_live_stats.py`: encounter open/close on the gap, rolling window pruning, averages.
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
2. Readout: `readout`, `live_stats`. Session with a map change or respawn. Gate: the three numbers show, update, and survive the controller change; menu toggle hides and shows them.
3. Report: `report.py`, `dmgreport`, tests, real fixture. Gate: report totals equal the readout's last encounter, `--last 300` sane, all tests green.
4. Packaging: `build.py`, Vortex end-to-end install. Gate: fresh install works from the zip alone.
5. Docs and the Nexus page text. Then tag `v1.0.0`.

One commit per step, messages in the existing style.

## Open points

- Nexus display name and author string for `pyproject.toml` (proposal: "BL4 Damage Tracker", author "SevenX").
- Default readout position: top left at (120, 320) worked in the probe; say if another corner is better.
- Whether `dmgreport` output to the SDK console is enough for the colleague, or the report should also open a text file.
