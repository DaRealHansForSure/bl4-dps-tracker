# Hook notes: what the mod relies on inside the game, and what to check after a patch

Everything here was established on the game build of 2026-09-19 with oak2-mod-manager v0.3 (unrealsdk 1.10 / pyunrealsdk). The raw record, including every dead end, is `poc/findings.md`; this file is the curated version for whoever has to fix the mod after a game update.

## The mechanism

BL4 applies damage natively. No UFunction fires per hit: 170 `Damage`-named functions were hooked and a full call trace over a gunshot showed nothing. What works instead:

- `OakPlayerController.RecentlyCausedDamageInstances`: a fixed array of six `RecentPlayerCausedDamageInstance` structs, written round-robin, the source of the HUD's floating numbers. Each write increments the slot's `RepDirtyFlag` (a byte, wraps at 256).
- The mod hooks `/Script/GbxUIUMG.GbxUIUMGTickWidget:BP_TickWidget` (POST; 60-100 calls per second, once per frame) and compares the six flags with the previous frame's values. Every changed slot is a new damage instance.
- Fields read per instance: `TotalDamage` (float, matches the on-screen number), `DamageType` (an `FGameDataHandle`: `_name`, `bIsDoT`, `bIsHealingType`, `ElementalType`; some handles are unresolved and raise on field access, so fields are read with defaults), `DamagedActor` (weak actor, `Name`), `DamageFlags.Flags` (uint32), `RepDirtyFlag`. `DamageSource` is always empty in this replicated copy.

Settings `tick_function`, `ring_property` and `ring_size` in `sdk_mods\settings\dmgtracker.json` hold the two paths and the ring size. After a patch, the mod checks the tick function at enable and the ring property at the first controller in the world, and logs one clear error instead of failing every frame.

## Flag bits (`DamageFlags.Flags`)

The reflected enum `ERecentPlayerDamageFlags` lists bit indexes: CriticalHit=1 (`0x2`), Healing=2 (`0x4`), Shield=4 (`0x10`), Resist=8 (`0x100`), DontShowCrossHairHitMarker=16 (`0x10000`). Two bits are not in the enum and were decoded from recorded sessions:

- `0x400` (bit 10): set on bullet impacts and sticky detonations, never on grenades, melee, splash, elemental or explosive-round damage.
- `0x4000` (bit 14): set on exactly the hit that kills the target (33 of 33 tracked targets across three sessions).

## Attribution rules, in order

1. Healing (`0x4` or `bIsHealingType`) is dropped.
2. Status ticks (`bIsDoT`, or a damage type name ending in `_Status`; not all ticks have `bIsDoT`) go to the source of the last non-status hit on the same target within 30 s.
3. A hit with `0x400` goes to the gun in hand (`pc.Pawn.ActiveWeapons.Slots[0].Weapon`), unless it lands within 0.75 s after a weapon swap on a target the previous gun hit in the last 10 s and the new gun has not hit that target yet: then it is a sticky that the swap detonated, and it goes to the previous gun.
4. An unflagged hit on a target that took a `0x400` hit in the same frame is the elemental half of that bullet: gun in hand.
5. An unflagged hit within 0.25 s of a `/Script/GbxGame.projectile:Explode` whose object's `GetOwner()` is an `OakWeapon` goes to that weapon (explosive rounds, whatever is in hand). An explosion owned by the character is a grenade. Unexplained hits are held for 100 ms because the damage can land a frame before the event.
6. Otherwise the melee window (from a `/Script/GbxGame.GbxTrickScript:OnBegin_Mut` on an `ATrick_Melee*` trick until its `OnEnd_Mut` plus a few frames), then the skill window (3 s after any trick with `ActionSkill` in its class name begins or ends, after any `/Script/GbxGame.GbxSkillScript:K2_StartSkillAction` on a script whose `GetOwner()` is the player controller, or after `/Script/OakGame.GbxSkillTokenProviderStatics:AddToTokenStack`; every hit credited to the skill keeps the window open 1.5 s more, so it follows the skill's own damage), then the grenade window (12 s after `/Script/OakGame.OakCharacter:ThrowGadget`, refreshed 3 s by `/Script/OakGame.GrenadeGadget:ExplodeCallback` or a character-owned explosion).
7. Anything left is `Other`.

Enemy melee tricks are `TRAnim_<Enemy>_...` and are instanced on the enemy; the player's `ATrick_Melee*` scripts are shared and cannot name their actor, so the prefix is the marker. The `Script_LTrick_HandleOffhand` trick under `Tricks/Skills/` fires for other off-hand actions and is not a melee signal.

## Weapons and characters

- The weapon in hand: `pawn.ActiveWeapons.Slots[0].Weapon`. Equipped weapons: `pawn.EquippedInventorySlots.items[i].InstancedInventory` with class `OakWeapon` (the same array holds Shield, GrenadeGadget, RepairKit). Both are empty right after a world load and fill once the pawn is spawned.
- Label: the stem of `weapon.BodyData._path_name()` (`Body_JAK_SR` -> "Jakobs Sniper"; codes seen: VLA, JAK, TOR, BOR, DAD). Identity: `weapon.Item.data.InstanceId`, stable across game restarts. There is no runtime display name: `Item.data.Identity` is 216 opaque bytes, the HUD is Coherent with no reflected text, and the item libraries work on loot definitions.
- A vehicle pawn (`OakVehicle`) has no `EquippedInventorySlots`; the mod hides the table and ignores hits while the pawn is not the character.
- Character: `pc.PlayerState.ActiveCharGuid` (four ints, per save), `ReplicatedCharacterDef` (`Char_DarkSiren`), `pawn.UXNameRepData.DisplayDef.Text` for the UI name.

## The on-screen table

Runtime UMG: `WidgetBlueprintLibrary.Create(pc, GbxUIUMGUserWidget, pc)` (UserWidget itself is abstract), a `Border` root with a `GridPanel`, one `TextBlock` per cell (`SetFont` with a `SlateFontInfo` on `/Engine/EngineFonts/Roboto.Roboto`, `SetJustification`, `SetMinDesiredWidth`, `SetColorAndOpacity`, `SetShadowOffset`, `SetShadowColorAndOpacity`), `AddToViewport(10000)`, `SetPositionInViewport`. Canvas text via `HUD:ReceiveDrawHUD` never fires in BL4.

Hide rule: no pawn, a vehicle pawn, `pc.bShowMouseCursor` (every menu), `pawn.bHidden` or `pc.IsMoveInputIgnored()` (tunnel transitions, cutscenes).

## Safety rules learned the hard way

- Never keep a controller or pawn wrapper across frames: reading through one after a world teardown crashed the game (access violation). Fetch `get_pc()` every tick and compare `_get_address()`.
- Never read hook arguments of unknown property types: reading `ThrowGadget`'s arguments crashed the game. Hooks read the object and call no-argument getters only.
- The widget is destroyed with its world behind the mod's back; check `unrealsdk.find_object(class, path)` before touching it.
- Blueprint functions load with the world; register hooks on them without checking existence (unrealsdk matches by name at call time).
- Walking every property of a pawn, or all classes, from a probe froze the game; keep probes narrow.

## After a patch: the checklist

1. Does the SDK load (`mods` in the console)? If the console does not open, the SDK itself needs the update, not this mod.
2. Log window: `[dmgtracker] tick function ... does not exist` means `BP_TickWidget` moved; find a per-frame function in a call trace (`poc/logall_on.py` / `logall_off.py`) and set `tick_function`.
3. `tracking stopped: property RecentlyCausedDamageInstances is missing` means the ring moved; look for a six-element struct array on the controller with `RepDirtyFlag` (`poc/probe_weapon.py` style dumps) and set `ring_property` / `ring_size`.
4. Wrong weapon rows: check the body path stem format and the `ActiveWeapons.Slots[0].Weapon` path (`poc/probe_weapon.py`).
5. Wrong Grenade/Melee/Skill rows: the hook paths in `dmgtracker/mod.py` (`SOURCE_HOOKS`) or the trick naming; `poc/probe_tricks.py` and `poc/probe_source.py` print what fires.
