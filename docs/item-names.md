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
equipped gun is in two of them the mod reads (`poc/probe_run49_around.py` to
`probe_run57_slots_and_headers.py` and runner runs 62-77, sessions 3 to 6):

- **Item cards**, one per equipped weapon in slot order, 280 bytes: the slot type (`weapon`),
  the name (with a trailing space), the thumbnail path
  (`TextureRenderTarget2D'/Engine/Transient.SceneCaptureComponent2D_...:OakInventoryThumbnail_...'`),
  the element, the damage text (`1.612 x 10`), the item serial (`@Ugd_t@Fme!K...`) and the
  rarity. No seed. They exist from the first second in the world.
- **Inventory list**, one per item the inventory screen last showed (22-25 records, backpack
  and equipped, 1072 bytes): the seed (+632) with the name (+80), the serial (+392), the
  slugs and the localised texts. A snapshot: it appears once the inventory screen was
  opened and misses a gun equipped from the ground since.

A third model, the slot records (288 bytes: type slug, rarity slug, a copy of the identity
with the seed at +248, slot order), exists too but leads nowhere: the cards were 9 KB from
it in one session and hundreds of MB away in the next.

Nothing the SDK can name points at these arrays. A walk of 58k pointers from the UI
objects found none (`probe 50`), the `OakGameInstance+704` anchor of the first two sessions
(header node 2.3 KB and 5 KB away) was heap adjacency (104 MB away in session 3), and the
UE reflection globals (`GObjects`, `GNames`, `GWorld`, from the forum or Dumper-7) reach
UObjects only, which these structs are not. One lesson from runs 57-68: a pointer scan run
from Python finds its own needles too. The "binding caches" and "field-address maps" the
earlier notes described were `bytes` objects and other Python values of the probes (their
type pointers resolve into `python314.dll`); the only real holder of the cards' data
pointer is the array header itself.

So `dmgtracker/itemnames.py` finds both models by content, in one pass on a thread of its
own:

1. The seed of an equipped gun is known from its actor. Every committed private
   read-write region (7.3 GB, 52k regions) is read a megabyte at a time (8 s for all of it
   once the read buffer is copied by its read length, `ctypes.string_at(buffer, got)`;
   copying an 8 MB `buffer.raw` for each of 45k tiny regions is what made earlier scans
   take minutes). The regions within 512 MB of the last model found come first, or,
   knowing none, the band 0x300000000-0x380000000 where the UI's allocator put the models
   in every session seen (cards' node at 0x31ab9e8d0 and 0x31f312510, lists at 0x31..,
   0x33..); the rest follows, so a wrong guess only costs the order.
2. A hit of the seed is tried as an inventory record (seed at +632): the whole list's names
   are taken, no confirmation needed. That covers every gun the inventory screen showed.
3. The cards are found through the node that holds their array header, which had the same
   shape in every session: a one-element array header `{ptr, 1, 1}` right before
   `{data, num, max}`, two ints, an 8-byte zero and an 8-byte minus one after it. The
   search needle is the `{1, 1}` without its trailing zeros (`bytes.find` degenerates on a
   needle ending in a byte as common as zero: a pointer scan for `0x329d84d8` took 27 s,
   for the same value without the zeros 10 s), the rest of the shape is checked on the
   chunk bytes without a read, and only then are the records behind the pointer read and
   validated (slot type, name, serial `@U...`, rarity). Live: 1.6 s to the node, in the
   band. Searching `{ptr, N, N}` headers instead cost 264 s (512k matches, 334k reads).
4. Card i pairs with the gun in slot i, the order of the game's own `EquippedInventorySlots`
   that the caller passes as seeds; empty slots are absent on both sides. A pairing is
   stored only when two reads a second apart agree, because the model lags an equipment
   change by a moment. The addresses are remembered; a new gun is read from them without a
   scan, and a scan that found nothing rests 15 s, then twice as long each time (ten
   minutes at most): the first scan of a session, seconds after the character loaded,
   usually runs before the UI has built its models.

The mod asks the reader only while the player has been in play for three seconds (a pawn,
no menu, controls not taken by a tunnel or cutscene, `mod.py` `_in_play`): a scan during a
load finds nothing, because the UI builds its models a moment after the world is up.

The record layouts and the node shape are keyed by the game build
(`KismetSystemLibrary.GetBuildVersion()`, `Oak2-Win64-4845623-CL-4845623`); an unknown
build is tried with the latest layout, and the validation decides. Names are stored in one
table for every character, `BL4DamageTracker\item-names.json` (`dmgtracker/itemnamestore.py`,
item id -> name): a gun's name depends on the gun alone, so a name read once serves every
character, survives the record resets, and is written on the log writer's 30 s interval
whether or not a hit was dealt. The per-character `weapons.json` holds numbers only; the
dashboard, `dmgweapons` and the report look the names up by item id.

Every read goes through `ReadProcessMemory` after `VirtualQuery` confirmed the pages are
committed and not guard pages: a read that touches a guard page disarms it, and the thread
whose stack it guarded crashes later. The scan pauses every 8 MB; the game thread only ever
reads remembered addresses.

## Gadgets

The grenade, the shield, the class mod, the repair kit and the enhancement sit in the
same slot array as the guns (`EquippedInventorySlots`, `attribution.equipped_gadgets`) and
have item ids the same way. Only the inventory list names them (they have no item card),
so the mod asks the reader for them (`names_of_items`) only once a menu with the cursor
has been open in the session, which is when the list first exists; a list-only scan (no
node search) finds it, 3 s live. The equipped grenade's item id is handed to the poller
(`set_grenade`), so grenade hits carry it (`wid`) and the dashboard names the Grenade row
and the grenade's hits like a gun's. Pictures are exported for gadgets the same way.

## Thumbnails

The card's render target (`OakInventoryThumbnail_...`, 512 x 256, RGBA16f) is exported
with `KismetRenderingLibrary.ExportRenderTarget`, which writes an OpenEXR (ZIP, half
floats); `dmgtracker/thumbnails.py` decodes it with the standard library into
`BL4DamageTracker	humbnails\<item id>.png`. The export flushes the renderer (10 ms), so
it runs only while a menu with the cursor is open, one item per refresh; the conversion
runs on the writer thread and yields every scanline. The `OakThumbnailManager` keeps the
targets in `RenderedThumbnails`, a `TMap` pyunrealsdk cannot read; its raw elements are
`{key 8 bytes, UTextureRenderTarget2D*, hash links}` with a key that is neither the
instance id nor the seed (not needed: the card names the target).

Not covered: backpack items (no actor, not needed for damage).

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
