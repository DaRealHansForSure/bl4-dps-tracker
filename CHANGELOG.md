# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.1] - 2026-10-25

### Added

- `README.md` in the layout of an open source project, with screenshots of the dashboard,
  `SECURITY.md`, `LICENSE` (MIT), this changelog and a logo.

### Changed

- The dashboard page (`dmgtracker/web/index.html`) follows the project's code style: four-space
  indentation, braces on every block, no compound or nested ternaries, one promise step per line.
- Conditional expressions in `mod.py` and `itemnames.py` that tested a compound condition or ran
  over several lines are plain `if` statements.

## [1.3.0] - 2026-09-22

### Added

- Gun pictures on the dashboard, rendered the way the inventory draws them.
- An item card on hover: type, rarity and element as badges, the card's damage figure, the item code.
- Rarity colours as the game paints them, element and damage type badges in the hit lists.
- A clickable chart legend, fights shaded on the chart, a tooltip on every table header.
- Lifetime records and a ranking for grenades.
- Every hit records the rule that claimed it; the hit lists show and search it.
- An action skill that takes the gun out of your hands is credited to the skill.
- Guns keep their names in co-op, by slot instead of by item.
- Developer tools in `poc/`: reload the mod without restarting the game, record a fight's
  attribution inputs and replay them, find misattribution in a session log.

### Changed

- The DPS column is damage per second right now, over the last 10 s.
- The totals cover the last hour of play; a pause is a pause.

### Fixed

- A burning target keeps its grenade, and the skill window can no longer feed itself.
- A grenade that went off outranks the skill window a passive skill keeps open.
- One burst of damage keeps one name.
- With other players in the session, action skill tricks are ignored, since they name no owner.
- Pictures work in a session without item identities.

## [1.2.0] - 2026-09-21

### Added

- Item names of the equipped guns, read from the game's item cards and kept in one table for every
  character.
- `dmgreset --names` and `dmgreset --names --all` forget the stored item names.

### Changed

- The item name scan reads the memory regions near the last model found first.

### Fixed

- The dashboard chart names its lines after the item once the name is known.
- The chart's "now" line keeps falling after the last hit.
- The table's window runs on wall time, in menus and pauses too.

## [1.1.0] - 2026-09-21

### Added

- Web dashboard served by the mod on the vendored `websockets` package: live table, chart, hits,
  weapons and encounters.
- `Best DPS` column, `Crit [%]` and `Kills` columns, row flash on growing totals.
- `dmgstatus`, `dmgreport --top/--all`, `dmgweapons --by`.

### Changed

- Every table row is damage over the window time; the window runs on play time.
- The in-game table defaults to Weapon, Total, DPS and Best DPS.

### Fixed

- Weapon records are keyed on the item's seed bytes, which survive restarts.
- Token skills and skill script actions feed the Skill row.
- Hits of one burst keep their order everywhere.

## [1.0.0] - 2026-09-20

### Added

- Mod core: per-frame diff of the controller's damage ring, attribution, JSONL session log, options.
- On-screen per-weapon damage table over a rolling window, hidden in menus, vehicles and tunnels.
- Attribution of explosive rounds, sticky detonations on weapon swap, status ticks, grenades, melee
  and action skills.
- Per-character lifetime weapon records with peak DPS in hand.
- `dmgweapons`, `dmgreport` and `dmgreset` console commands, `report.py` for offline reports.
- `build.py` produces the `.sdkmod` and the Nexus zip.

## [0.1.0] - 2026-09-20

### Added

- Proof of concept: per-hit damage data is reachable, matches the on-screen numbers, and can be
  written to disk and drawn on screen without a stutter.

[1.3.1]: https://github.com/DaRealHansForSure/bl4-dps-tracker/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/DaRealHansForSure/bl4-dps-tracker/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/DaRealHansForSure/bl4-dps-tracker/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/DaRealHansForSure/bl4-dps-tracker/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/DaRealHansForSure/bl4-dps-tracker/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/DaRealHansForSure/bl4-dps-tracker/releases/tag/v0.1.0
