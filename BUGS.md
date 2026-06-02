# BUGS.md

Findings from analyzing the `saves/demo_*` smoke-test artifacts (10 feature demos ×
4 runs each = 40 trace+save pairs, produced by `smoke_test.py`). Each demo run is a
live-model playthrough of a scripted scenario; the `*_stats_trace.json` sidecars hold
the per-turn tool calls and API stats, and the `*.json` saves hold the final
`GameState`.

Scope of the sweep: rule-enforcement correctness, game-engine state coherence, and
failed/aborted API calls.

---

## 🔴 Engine bug — `move_scene` mid-combat leaves a stale `combat_order` (reproducible 4/4)

**Status:** ✅ FIXED — `move_scene` now refuses while `combat_round > 0`
(`reason: "in_combat"`), so the move never happens and combat state cannot go stale.
Guard added at the top of the `move_scene` dispatch branch (covers named-scene and
free-form modes); `SYSTEM_PROMPT` COMBAT FLOW updated; regression test
`test_move_scene_declared_exit_refused_in_combat` in `tests/test_scenes.py` (full no-API
suite green).

The `demo_social_companions` smoke fixture that triggered this (it tried to walk out of
a live fight) is also ✅ FIXED: the root cause was the seed-42 parley *failing* — the
first d20 of the session is the persuade (rolls 4, +CHA 4 = 8) and Snik's
`disposition_dc` was 13, so the parley auto-started a fight the script then tried to
leave. Lowered Snik's `disposition_dc` to 8 in `data/demos/demo_social_companions.json`
so the seeded parley deterministically *succeeds*, letting the demo exercise its actual
feature (`recruit_npc` + companion-following) and move scenes out of combat as
documented; `data/DEMOS.md` updated to match. Verified end-to-end at the tool level under
seed 42 (persuade → recruit → move/follow → immovable skeleton → start_combat with Snik
allied). The script text itself was already the correct happy path and is unchanged.

**Severity:** high — produces an incoherent, effectively unwinnable combat state.

**Where:** the `move_scene` branch in `src/tools.py` (`dispatch`).

**Reproduction:** all four `demo_social_companions` runs (`_0`–`_3`) end in the
identical broken state. From `saves/demo_social_companions_0.json`:

```
scene=warren  combat_round=2  combat_index=2
combat_order=['wisp','snik','aldric']
   dangling=['snik']                       # snik is not in npcs/party
   hostiles_not_in_order=['rattle','grub'] # the scene's real enemies
game_over=False
```

**Root cause:** combat starts in the bridge scene (turn 1: Wisp's persuade of Snik
fails → auto-combat with order `['wisp','snik','aldric']`). At turn 3 the party moves
into the `warren` *while `combat_round` is still > 0*. `move_scene` rebuilds
`state.npcs` from the destination roster (`rattle`, `grub`) and drops Snik — **but
never touches `combat_order`, `combat_round`, or `combat_index`.** Combat therefore
continues with an order that points at a combatant who no longer exists and excludes
the enemies who actually do.

**Observed cascade** (from the `demo_social_companions_0` trace):

- `next_turn` advances to the dangling key `snik` → `all_actors.get('snik')` is `None`
  → `resolve_npc_action(None, …)` returns `None` → the engine falls back to the model,
  which calls `attack({'attacker':'Snik'})` → **`"Unknown attacker"`**.
- The real enemies aren't in `combat_order`, so `_resolve_offensive_target` excludes
  them (`combat_round != 0 and key not in combat_order`) → `attack(Aldric)` returns
  **`no_target`** even though Rattle is present; the model must name every target
  explicitly.
- `_maybe_end_combat` sees living hostiles (`rattle`/`grub`) and a living party, so
  **combat never auto-ends** — `game_over` stays `False` and the demo cannot conclude
  (all 4 runs `no-end`).

**Fix options** (small, localized to the `move_scene` branch):

- **A — refuse:** reject `move_scene` while `combat_round > 0`
  (`reason: "in_combat"`), mirroring how `recruit_npc` already refuses mid-combat.
  Forces the fiction to resolve the fight (flee/disengage) first.
- **B — reconcile:** treat a scene change as ending the current fight — run the
  `end_combat` cleanup as part of the transition (you can't be in the *same* fight in
  a new room).

Recommend **B** as truer to play, paired with a no-API regression test asserting
`combat_order`/`combat_round`/`combat_index` are cleared after a mid-combat
`move_scene`.

**Verification query** (flags any save whose `combat_order` has dangling keys or omits
a living hostile):

```python
import json, glob, os
for sf in sorted(glob.glob('saves/demo_*.json')):
    if sf.endswith('_stats_trace.json'): continue
    d = json.load(open(sf))
    if d.get('combat_round', 0) > 0:
        actors = set(d.get('party', {})) | set(d.get('npcs', {}))
        order = d.get('combat_order', [])
        dangling = [k for k in order if k not in actors]
        living_hostiles = [k for k, n in d.get('npcs', {}).items()
                           if n.get('hostile') and n.get('hp', 0) > 0]
        missing = [k for k in living_hostiles if k not in order]
        if dangling or missing:
            print(os.path.basename(sf), 'dangling=', dangling, 'missing=', missing)
```

Only the four `demo_social_companions` saves trip it; every other demo's combat state
is clean.

---

## 🟡 Soft-rule deviation — `demo_flat_effects_3` skipped a trap consequence

**Severity:** low — not engine-enforceable by design; a model-consistency gap.

At turn 2 ("Aldric lays his hand on the warding runes"), the model rolled an INT
`skill_check` (failed, 5 vs DC 13) and then **applied no damage at all** — whereas
runs `_0`/`_1`/`_2` correctly applied the flat `modify_hp -6` for the same scripted
input under the same dice seed. The engine cannot catch this: applying a failed-check
consequence is the model's job (the documented "model must apply the consequence" soft
boundary). Worth noting only because the same scenario silently no-ops a hazard in one
run out of four.

---

## ⚪ Not bugs (engine working as intended)

- **17 turn-guard rejections** (`"It is not X's turn"`), mostly in `demo_combat` and
  `demo_death_saves`: the smoke-test *scripts* name a character whose turn it isn't.
  The dice seed fixes initiative identically every run (`Wisp` first), but the scripts
  assume a different order. The engine correctly refuses and keeps the turn alive —
  this is **smoke-script desync, not an engine fault**, though it suggests those
  scripts should be re-synced to the seeded initiative order.
- All other `ok=false` results are correct enforcement: `ambiguous_target` ×10,
  `combat_starting` ×8, `locked` ×4, `already_attempted` ×4, `no_target` ×4,
  `slots_full` ×2, `immovable` ×1.

---

## ✅ What's healthy

- **No failed API calls.** All 742 recorded calls report usage with 100% cache hits;
  every run completed its scripted turns. (Caveat: the stats sidecar only records
  *successful* calls, so a fully-aborted turn would be absent — but every run produced
  a coherent final save, so there's no evidence of one.)
- **Spell-slot economy holds** — Pearl of Power refused at full slots (`slots_full`),
  `magic_missile` decremented to 0; no leveled spell cast at L0.
- **Death-save cycle coherent** — no PC dead-but-`hp>0`, no successes/failures outside
  `[0,3]`, no `hp > max_hp` in any save.
- **HP atomicity** — no negative HP in any result; `heal` reports the capped amount
  (`healed: 6`, not the raw `8`, in `flat_effects_3`).
- **Hazards** route through `trigger_hazard` (engine owns DC/damage); save-for-half
  math correct (`spore_cloud`: failed → 2, saved → 1).
