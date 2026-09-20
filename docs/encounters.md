# How a fight (encounter) is detected and counted

The mod never asks the game what a fight is. It only sees the damage you deal, one instance at a time, and groups those instances by time. Everything below follows from that.

## Detection: the gap rule

- A fight **starts with the first damage instance you deal**: a bullet impact, a status tick, a skill, grenade or melee hit. Healing never produces an instance, and damage dealt from a vehicle is dropped before it gets here, so neither starts a fight.
- It **stays open while hits keep coming**.
- It **ends when `encounter_gap_s` seconds pass without any hit** (default 15 s, a setting). Two things check that:
  - every new hit: a hit that arrives more than the gap after the previous one closes the old fight and starts a new one;
  - the refresh loop, every 250 ms, so a fight also ends on screen without waiting for another hit.
- The gap is measured on the wall clock. A pause menu or a loading screen longer than the gap ends the fight; nothing was happening during it anyway.

Nothing else defines a fight: not the number of enemies, not their identity, not your health. Consequences worth knowing:

- Two groups of enemies fought within 15 s of each other are one fight.
- A boss with a phase of more than 15 s in which you cannot damage it becomes two fights. Raise `encounter_gap_s` for such bosses; `dmgreport --gap 30` recomputes a recorded session with another gap without changing the setting.
- The default was 5 s at first; that ended fights during weapon swaps and grenade throws in real sessions, hence 15 s.

The code is `EncounterTracker` in [`dmgtracker/stats.py`](../dmgtracker/stats.py): `add(event)` applies the rule per hit, `close_if_idle(now)` is the 250 ms check.

## What is counted per fight

| Field | Meaning |
| --- | --- |
| started, ended | time of the first and the last hit |
| duration | ended minus started, at least 1 s (so one big hit reads as "that much per second") |
| total | all damage: direct hits and status ticks |
| hits | number of damage instances, ticks included |
| crits, kills | instances that carried the crit or the kill flag |
| dot_damage | the status-tick part of total |
| per source | total and hits for each gun (by item), Skill, Grenade, Melee, Other; a status tick counts for the source that applied it |
| targets | hits per enemy family (`Char_CatAdult_2147476409` counts as `CatAdult`) |
| active seconds | the distinct seconds in which at least one hit landed |

Two rates come from that:

- **DPS** = total / duration: the honest number for the whole fight, including reloads, walking and waiting.
- **Active DPS** = total / active seconds: the rate while you were actually landing hits.

The table's DPS column is a third thing: each row's damage over the rolling window's fighting time; it belongs to the window, not to encounters, see below.

## Where fights show up

- **In the game**: the optional `Encounter` row of the table (`readout_show_encounter_row`), showing the open fight or, between fights, the last one: total, DPS, and 100 % share of itself. Off by default because in the open world fights are short and many; it is meant for bosses.
- **`dmgreport`** in the console and `report.py` offline: one block per fight with duration, DPS, active DPS, crit share, kills, the top targets and the per-source split; the ten largest fights per character by default, `--all` for every one, `--gap` for another boundary. The session file's meta line records the gap the mod used, so the report reproduces the same fights unless told otherwise.
- **The dashboard**, Encounters tab: the same per fight, newest first, for any kept session, with the current `encounter_gap_s`.
- **`dmgstatus`** does not list fights, but `dmgweapons` and the weapon record are independent of them (they count every hit and every second in hand, fight or not).

The mod keeps the last 1000 closed fights of a session in memory for the in-game row; the report and the dashboard read the session file, so nothing is lost after that.

## The play clock, which is related but not the same

The table's other rows ("last 5 minutes of play") do not use encounters. They use a **play clock** that advances whenever you are in play, fighting or not: with a pawn, not in a menu (the pause menu included), not in a tunnel transition; a step longer than one second (a loading screen) is dropped. On that clock the rolling window covers the last `readout_window_minutes`, so a source that stops dealing damage comes down as its hits age out, while menus and pauses leave the numbers where they are.

Encounters still matter for one number: a gun's **time in hand** accumulates only while a fight is open and the table is visible. It feeds the lifetime record's DPS in hand (`dmgweapons`, the dashboard's Weapons tab), not the table. Fifteen seconds after your last hit the fight closes and the time in hand stops counting; the window's clock keeps running.
