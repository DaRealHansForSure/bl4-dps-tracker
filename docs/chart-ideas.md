# Chart ideas: what the data allows, ranked

Written 2026-09-22 as a survey of what would be technically possible and a big
improvement to the dashboard's charts. Nothing here is built; the ranking is by value
against effort, and each entry says what it needs.

## What exists

The Live tab draws one chart: one sample per second (`Dashboard._sample_if_due`), the
"now" DPS of the last 10 to 120 s per source stacked under the total, the fight's DPS
dashed, the fights shaded, a tooltip per sample, a legend that hides lines, 300 samples
kept (5 minutes). The Encounters tab is a list. The Weapons tab is tables.

## What the data already holds

Every hit in the session log: time, damage, direct or status tick, crit, kill, source
kind, gun (item id), damage type and element, the target actor's name (spawned enemies
carry an instance suffix, `Char_TargetDummy_2147473822`, so one enemy can be told from
the next), the game's flags. Held lines: seconds in hand per gun. Character markers.
The poller also knows, but does not write: skill activations (the skill window), grenade
throws (`ThrowGadget`), weapon swaps (the gun in hand per tick). The page gets hits live
over the WebSocket and can page through any session's hits (`/api/hits`).

## Ideas

### 1. Marks on the live chart: what made that spike (small, high value)

Draw each hit as a dot on the chart at its time, size by damage, a ring for a crit, a flag
for a kill, in the source's colour; hover a dot for the hit. Add bands for the action
skill (the poller's skill window) and ticks for grenade throws and weapon swaps. Then a
spike explains itself: the skill was on, the grenade landed, the crit chain.

Needs: the poller writing three small event lines (`skill` start and end, `throw`,
`swap`) to the log and the dashboard; the page has the hits already. A day.

### 2. Fight detail and fight comparison in the Encounters tab (medium, high value)

Click a fight: its DPS curve in one-second bins from the session's hits, stacked by
source, the average as a line, the marks of idea 1, the kills. Above the list a bar per
fight (height DPS, width duration, colour by character) so a session's progression is
visible; pick two fights to overlay their curves.

Needs: `/api/encounter-timeline` computing the bins server side (cheap, the events are
parsed already) and a second use of `drawChart`. Two days.

### 3. A breakdown panel next to the chart (small, high value)

For the window or the current fight: damage by source, by element, direct against status
ticks, crit share, and by enemy family, as bars or a donut. Answers "how much of my damage
is the burn" and "which enemies soak it" at a glance.

Needs: nothing new from the mod; the page sums the hits it has. Half a day.

### 4. A session strip under the live chart (medium)

The whole session's DPS as a thin line with the fights as blocks; click or drag a range to
show it in the main chart, which today keeps five minutes.

Needs: `/api/session-timeline` (one-second bins over the session; cheap) and a range
mode for the chart. Two days. Idea 2's endpoint does most of it.

### 5. Damage per enemy and time to kill (medium)

The target's name identifies the enemy instance, so each enemy's first hit, last hit,
damage and kill are in the log: time to kill per enemy family, damage after the kill
(overkill, the last hit's excess), the enemies that took the longest. A table per fight
and a bar chart per family.

Needs: server side grouping by target name within a fight. One day. Exact overkill needs
the target's health at the hit (`DamagedActor.HealthState.HealthTypeStates[]`, seen in
findings.md, not mapped), which is a probe.

### 6. Hit-size distribution per gun (small)

A histogram of hit damage per gun, crits apart, on the Weapons tab when a gun is picked:
a Jakobs gun's spikes against a Vladof gun's stream, and whether the crits carry the
damage.

Needs: the hits of the character's sessions by item id (`/api/hits` with a query, or one
endpoint that bins). Half a day.

### 7. The personal-best ghost (medium)

Overlay the best fight's curve (the character's highest-DPS fight of at least 20 s) on
the current fight, like a racing ghost, so the live chart shows whether this fight runs
ahead or behind.

Needs: the best fight's one-second bins stored per character (a small JSON next to
`weapons.json`), aligned by fight start. One to two days.

### 8. Your own health and shield under the DPS (research first, potentially the biggest)

A second lane: the player's health and shield over the fight, from the pawn's
`HealthState` read once per tick (the same struct the target carries). With idea 1 it
shows why the DPS dropped: shield gone, retreat, revive. Damage taken per source is not
in the ring the poller reads; the health curve is.

Needs: a probe to map `HealthState.HealthTypeStates[]` (current, max, per type), then a
per-tick read (cheap reflection) and a sample field. Unknown until the probe runs; the
read itself is a few microseconds.

### 9. Export (trivial)

A PNG of the chart or of a fight (`canvas.toBlob`), and a copy of the fight's numbers as
text, for sharing. An hour.

## Recommendation

Ideas 1, 2 and 3 together change what the charts explain: not only how much, but why
and when. They need one small mod-side change (the marker lines of idea 1) and are
otherwise page and API work over data the log already has. Idea 8 is the one worth a
probe before deciding; idea 7 is the one that makes people play another round.

The chart engine stays the hand-written canvas: no dependency, works offline (the page is
served by the mod), and a few thousand points draw in a millisecond. Zoom and pan (idea
4) are one function on top of it, not a reason for a library.
