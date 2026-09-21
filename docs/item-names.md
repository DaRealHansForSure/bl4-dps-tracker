# Item names: where they come from and where the mod reads them

Research of 2026-09-21 (`poc/probe_names3.py` to `probe_names30.py`, run in the game with the
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

The UI is Coherent (HTML/JS). The six copies of the finished name a full memory scan found
were all UI-side (render commands with transform matrices, DOM objects, a text-shaping
record), and the UI Blueprints hold no text. The one place that ties the name to an item
is the UI's **item card model of the equipped weapons**: one 280-byte record per weapon
slot, in slot order, each holding as FStrings the slot type (`weapon`), the name (with a
trailing space), the thumbnail path, the element icon and element, the damage text
(`1.612 x 10`), the item serial (`@Ugd_t@Fme!Kx...`) and the rarity (`legendary`). A second
block right before it holds one 288-byte record per slot with a copy of the identity
(seed bytes included), and a third one (low heap, 0x1415...) lists every equipped item with
slugs (`Jakobs-Shotgun`, `legendary`, `jakobs`, `weapon`, `shotgun`, `206.771`).

The records array's header `{data, num, max}` sits in a 64-byte node of the UI model pool.
Its owner could not be named (the pointers to it are the JS binding cache and a Nexus
field-address map), but the pool is reachable: `OakGameInstance + 704` is the UI system
object, `+1176` inside it is the UI model table, a 64-byte allocation in the same pool.
`dmgtracker/itemnames.py` reads those two pointers, takes the memory region of the model
table and scans it (64 KB, then 1 MB, then 8 MB around the table) for a header whose first
record validates: slot type, a name, a serial starting with `@U`, a rarity. In the live
test that took 0.9 ms; a cached header reads in 0.2 ms. The offsets are keyed by the game
build (`KismetSystemLibrary.GetBuildVersion()`, `Oak2-Win64-4845623-CL-4845623`); an unknown
build is tried with the latest layout, and the validation decides.

The records are paired with the equipped weapons by slot order; a pairing counts only when
two consecutive reads (one second apart) agree, because the UI model lags an equipment
change by a moment. Names are stored in the weapon record (`weapons.json`, field `name`), so
a gun keeps its name across sessions even before the UI model is up.

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
  pages; full scans belong in a background thread (`probe_names21`, `25`), never on the
  game thread (the 45 s scan of `probe_names10` froze the game).
