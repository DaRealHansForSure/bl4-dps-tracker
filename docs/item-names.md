# Item names: where they come from and where the mod reads them

Research of 2026-09-21 (`poc/probe_names3.py` to `probe_names32.py`, run in the game with the
gun in hand). The rule for it: names must come from the running game, not from data
files. Everything below was read from the live process.

## What a weapon name is made of

`Maintained Screeching T.K's Wave` (Jakobs shotgun, legendary) is three parts, and none of
them is stored on the item:

1. **Licensed-part prefix** (`Maintained`). Every manufacturer has a data table
   (`JAK_Weapon_LicensedPartPrefix_Table`, a `GbxUEDataTableDef` in the definition arena):
   rows for the licensed part kinds a gun can carry (`Default`, `jak_barrel_acc`,
   `hyp_shield`, `licensed_ted`), columns for the licensed magazine (`default`, `tor_mag`,
   `cov_mag`, `borg_mag`) plus `use_payload_prefix`. The shotgun's magazine `JAK_SG_Mag_05` is a
   Borg part (its audio, effects and UI stat are all `ULP_Bor`, `lp_bor_mag`), row `Default`,
   column `borg_mag` = `Maintained`. The same table gives `Sudden`/`Cooking` for Torgue/COV
   magazines, `Kettled`/`Steaming`/`Drummed`/`Potted` with a Hyperion shield, and `-Tossed`,
   `-Put`, `-Cranked`, `-Tucked` for Tediore parts, which combine with the Tediore payload
   table (`Hand`, `Speed`, `Smart`, `Rocket`, `Swarm`, `Spray`, `Flank`, ...).
2. **Attribute prefix** (`Screeching`). The type's `NamingStrategyDef` is an
   `OakWeaponNamingStrategy` (an inline struct the SDK reads as `WrappedInlineStruct`):
   eight attributes with two thresholds each (`damage` 1.2/1.4, `critdamage` 1.35/1.5,
   `firerate` 1.15/1.25, `reloadspeed` 0.85/0.75, `magsize` 1.2/1.4, `Accuracy` 0.8/0.65,
   `elementalpower` 1.15/1.25, `adsproficiency` 0.8/0.65), one word per attribute over the
   first threshold (`Sinister`, `Hidden`, `Hissing`, `Wild`, `Patient`, `Scouted`, `Marshy`,
   `Stalking`), one over the second (`Nightmarish`, `Unseen`, `Snarling`, `Primal`,
   `Imperturbable`, `Hunted`, `Swampy`, `Hounded`) and one per pair over the first
   (`Screeching` = damage + firerate, `Looming`, `Panicking`, `Terrifying`, `Roaring`, ...
   28 pairs), with priorities that let a pair win over a single word. The attribute names map
   to real weapon attributes through `OakGlobalsInventory.InventoryNamingAttributes`
   (`damage` -> `weapon_damage`, `firerate` -> `weapon_fire_rate`, `Accuracy` ->
   `weapon_spread`, `reloadspeed` -> `weapon_reload_time`, `magsize` ->
   `weapon_max_loaded_ammo`, `critdamage` -> `weapon_damage_modifier_add_critical_hit`,
   `elementalpower` -> `..._base_status_effect_damage`, `adsproficiency` ->
   `weapon_zoom_duration`). The thresholds are ratios against the type's base values, which
   the attribute pipeline computes from every part's attribute effects; the "stat mod" parts
   (`Damage mod`, `Accuracy mod`, ...) only carry a constant 1.0 each.
3. **Title** (`T.K's Wave`). The barrel's `InventoryNamingAspect` lists it in `TitlePartList`
   as an `InventoryNamePartDef` (priority 3). The type's own prefix/title/suffix lists are
   empty and inherit from the base type; the 200 name parts the arena references are titles
   plus the prefixes of shields, grenades and class mods.

Reproducing step 2 would mean reproducing the attribute pipeline, so the mod does not
compose names. It reads the finished string instead.

## Where the finished name lives

The UI is Coherent (HTML/JS) and keeps C++ data models for its pages. The name of an
equipped gun is in three of them (`poc/probe_run49_around.py` to `probe_run57_slots_and_headers.py`, the
kept scripts of probe runner runs 49-57, session 3):

- **Slot records**, one per weapon slot in slot order, 288 bytes: the type slug (`shotgun`),
  the rarity slug (`legendary`) and a copy of the item identity, whose twelve seed bytes
  (record + 248) are the item id the mod keys everything on. No name.
- **Item cards**, one per weapon slot in the same order, 280 bytes: the slot type (`weapon`),
  the name (with a trailing space), the thumbnail path, the element, the damage text
  (`1.612 x 10`), the item serial (`@Ugd_t@Fme!K...`) and the rarity. No seed.
- **Inventory list**, one per item the inventory screen last showed (22 records, backpack
  and equipped, 1072 bytes): the seed (+632) with the name (+80), the serial (+392), the
  slugs and the localised texts. A snapshot: a gun equipped from the ground is missing
  until the screen is opened again.

Nothing the SDK can name points at these arrays: the holders of their data pointers are the
UI's binding caches and a field-address map (`probe 57`), a walk of 58k pointers from the
UI objects found none (`probe 50`), and the `OakGameInstance+704` anchor of the first two
sessions (header node 2.3 KB and 5 KB away) was heap adjacency: in session 3 the cards'
header sat 104 MB from it. The UE reflection globals (`GObjects`, `GNames`, `GWorld`, from
the forum or Dumper-7) reach UObjects only, which these structs are not.

So `dmgtracker/itemnames.py` finds them by content, on a thread of its own:

1. The seed of an equipped gun is known from its actor. A scan of every committed private
   read-write region (7.3 GB, 52k regions) for those twelve bytes takes 8 s once the read
   buffer is copied by its read length (`ctypes.string_at(buffer, got)`); copying an 8 MB
   `buffer.raw` for each of 45k tiny regions is what made earlier scans take minutes.
   The regions within 512 MB of the last model found come first, or, knowing none, the
   band 0x300000000-0x380000000 where the UI's allocator put the models in every session
   seen (headers and lists at 0x31.., 0x32.., 0x33..); the rest follows, so a scan that
   finds the models early ends early and a wrong guess only costs the order.
2. Every hit is tried as an inventory record (seed at +632: name at once) and as a slot
   record (seed at +248). The slot records and the item cards are allocated by the same
   system at the same time and lie in one heap block (adjacent in session 1, 9 KB apart in
   session 3), so the cards are found by content in a 512 KB window around the slot
   records; of several card arrays there, the one whose rarities match the slot records,
   nearest, wins.
3. Slot i pairs with card i. A pairing is stored only when two reads a second apart agree,
   because the models lag an equipment change by a moment; a name from the inventory list
   needs no confirmation. The addresses are remembered; a new gun is read from them
   without a scan, and a scan that found nothing rests 15 s, then twice as long each
   time (ten minutes at most): the first scan of a session, seconds after the character
   loaded, usually runs before the UI has built its models.

The mod asks the reader only while the player has been in play for three seconds (a pawn,
no menu, controls not taken by a tunnel or cutscene, `mod.py` `_in_play`): a scan during a
load finds nothing, because the UI builds its models a moment after the world is up.

The record layouts are keyed by the game build (`KismetSystemLibrary.GetBuildVersion()`,
`Oak2-Win64-4845623-CL-4845623`); an unknown build is tried with the latest layout, and
the validation decides. Names are stored in the weapon record (`weapons.json`, field
`name`), so a gun keeps its name across sessions before any model is found.

Every read goes through `ReadProcessMemory` after `VirtualQuery` confirmed the pages are
committed and not guard pages: a read that touches a guard page disarms it, and the thread
whose stack it guarded crashes later. The scan reads a megabyte at a time with a pause
every 8 MB; the game thread only ever reads remembered addresses.

Backpack items are not covered (no actor, not needed for damage). Reading the equipped
weapons' names means the mod never has to compose a name itself.

## Tools

- `poc/probe_runner.py`: `pyexec probe_runner.py` once; afterwards every `.py` dropped into
  `poc/inbox/` runs on the game thread in its own folder `poc/runs/<time>_<name>/` with its
  own `probe_output.txt`. `pyexec probe_runner_stop.py` removes it.
- Typed views on any definition address: write a triple `{0, UScriptStruct*, data*}` into
  the `BaseType` field of a scratch `InventoryTypeDef` made with `unrealsdk.make_struct`;
  the SDK then reads the data as that struct, inline structs included (`probe_names14`).
- FName from a raw index: write the 8 bytes into the `Name` field of a scratch
  `InventoryPartCriterionData` and read it back (`probe_names13`).
- Memory reads through `ReadProcessMemory` on the own process fail cleanly on unmapped
  pages; full scans belong in a background thread (`probe_names21`, `25`, `32`), never on
  the game thread (the 45 s scan of `probe_names10` froze the game). A read that starts on
  an unmapped page yields nothing at all, so scan in page-aligned chunks. Never read a
  guard page (`VirtualQuery` first). Dumper-7 must not be injected while playing: its
  dumper thread asserts the engine (`UObjectBase.cpp:153`, crash of 2026-09-21 22:23).
