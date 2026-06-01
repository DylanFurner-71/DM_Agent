# Design Decisions

A lightweight log of deliberate design trade-offs made in the DM Agent, so the
rationale travels with the code. Each entry records *what* was decided, *why*,
and what was knowingly given up.

**Shared principle.** Several of these decisions are safe only because the engine
is the source of truth. The authoritative game numbers (HP, slots, turn order,
combat state) live in `game_state` and are enforced in `rules`/`tools` — never in
the narration or the message history. That separation is what makes the agent
un-cheatable, and it's also what makes it optimizable: narration and transcript
history can be trimmed or regrouped without affecting correctness, because nothing
that matters mechanically depends on them.

---

## 1. Out-of-turn declared actions — discard and acknowledge

**Decision.** When a player declares an action for a character whose turn hasn't
come up (e.g. "Wisp casts magic missile" while it's Aldric's turn, or as the
combat-opening input when Wisp isn't first in initiative), the action is **not**
executed and the turn is **not** advanced. Instead the DM acknowledges it —
"initiative is Aldric → Wisp → Snik; Wisp is last, I'll prompt you for her when
her turn comes" — and the player re-enters the action when the order reaches that
character.

The alternative considered was to **queue** the declared action and auto-fire it
when the character's turn arrives. That was rejected.

**Why.** Discard-and-acknowledge matches standard D&D, where you choose a
character's action *on their turn, with current information*, rather than
pre-committing. Queuing introduces staleness: by the time the queued action fires,
the board has changed — the target may be dead, the caster downed, or the player
may simply want something different — and handling all of that is exactly where the
queue approach's bugs would live. Discard-and-acknowledge is also the smaller,
lower-risk build.

**Trade-off.** The player sometimes re-types an action they already expressed once,
which can feel slightly redundant. The acknowledgment line buys that back so nothing
feels silently swallowed. We accept minor repetition over the staleness and
edge-case complexity of auto-firing.

**Scope.** Turn order and resolution are unchanged; this only governs how a
not-yet-actionable declaration is handled. Critically, the active player character
is never skipped in order to chase the character the input named.

**Reversibility.** Localized to the turn loop and system prompt. A later middle path
— showing the declared action as a *confirmable default* on that character's turn —
remains open without committing to full auto-fire.

---

## 2. Batch NPC turn narration into a single model call

**Decision.** Combat narration is split: the player's own action is narrated in its
own dedicated call, while all auto-run NPC actions for the cycle are narrated
together in **one** call (fed an ordered list, one beat per action), instead of one
narration call per resolved action.

**Why.** Per-action narration made model round-trips scale with combatant count, so
a two-goblin room ran noticeably slower than a one-goblin room. Batching makes NPC
narration a constant single call regardless of how many NPCs act, cutting per-round
latency and token cost.

**Trade-off.** This trades a reliability guarantee for speed. Per-action narration
was introduced specifically to stop the model dropping, merging, or reordering
beats — when each call holds exactly one beat, it physically can't. Batching NPC
beats back into one call reintroduces some of that risk *for NPC actions*: the model
could drop or merge a goblin's beat even though the engine applied its effect,
producing a narration/engine mismatch (HP changed in state, no prose explaining it).
We accept the weaker guarantee for NPCs but deliberately keep the player's own action
on the strong, per-action path — a dropped player-action beat is the worst version of
that bug.

**Scope — what's unaffected.** Purely a narration-presentation change. Mechanical
resolution is untouched: each NPC action still runs its own tool call, its own
`next_turn`, and all enforcement (turn ownership, action economy, rolled == applied).
Engine state stays authoritative regardless of how narration is grouped, so the worst
case is incomplete *prose*, never wrong *numbers*.

**Guardrails.** Feed the batched call the explicit ordered list with "one beat per
action, in order"; keep the mocked-client test asserting the batched call receives
every NPC action in resolution order; optionally add a cheap check that the beat
count matches the NPC-action count, to catch a silently dropped beat.

**Reversibility.** Localized to the narration step in `dm_agent.py` — easy to revert
to per-action, or make it conditional (batch above N NPCs, per-action at or below).
Low lock-in; revisit if large multi-enemy encounters appear, where drop/reorder risk
grows with batch size.

---

## 3. Bound the per-turn model context

**Decision.** The agent builds a fresh, bounded context each turn — system prompt +
a current-state snapshot from `get_state` + a rolling window of the last N narration
exchanges + the new input — instead of accumulating and resending the full
conversation transcript every turn.

**Why.** With an append-only message history, per-turn cost and latency grew with
session length: every call carried all prior narration and tool traces, so calls got
heavier the longer you played. Bounding the context keeps per-turn cost roughly flat
no matter how long the session runs.

**Trade-off.** Trading long-range narrative memory for bounded cost. The model sees
only recent narration plus current state, so it won't recall verbatim details from
many turns ago — a line an NPC dropped two scenes back, a description the player
latched onto — unless they're captured in state. For *mechanical* correctness this
costs nothing, since the engine owns the authoritative state and we inject it fresh;
the loss is purely *story* continuity for callbacks beyond the window. Acceptable
because this is a single-session crawl where everything that mechanically matters
lives in state, not in prose.

**Scope — what's unaffected.** Correctness is untouched: because the state snapshot
is injected every turn, the model always acts on accurate HP/slots/combat/scene
regardless of what narration was trimmed. The `/trace` log and the save file are
separate and remain complete — trimming only affects the *model's* context, not what
the engine records or persists. Within a turn's tool-use loop, that turn's tool
results stay intact, so multi-tool turns still resolve coherently.

**Guardrails.** The rolling window preserves short-range continuity so combat and
dialogue flow turn-to-turn (tune N to about one combat round's worth of exchanges);
the state snapshot guarantees mechanical accuracy; a no-API test asserts the built
context stays bounded as history grows and always contains the latest snapshot.

**Reversibility.** Localized to the context-builder in `dm_agent.py` — easy to widen
the window, revert to full history, or later add a running "adventure so far" summary
for long-range callbacks (at the cost of one extra maintenance call). Low lock-in.

## 4. NPC weapons are engine-selected, not model-named

**Decision.** For NPC attacks, the engine picks the weapon from the NPC's statblock
(the first melee weapon in its inventory); the model names only attacker and defender,
never the weapon. PC attacks are unchanged — the player names the weapon, validated
against inventory.

**Why.** The model has strong D&D priors and will supply a plausible weapon whenever it's
left to name one. A goblin whose defined inventory was shortsword/shortbow was attacked
with a "scimitar" — the canonical 5e goblin weapon, pulled from the model's training,
present in neither the statblock nor the WEAPONS table. Plausible and even rules-correct,
but data the engine never authorized. Taking the naming away from the model closes the gap
so the prior has nowhere to leak in.

**Distinction.** Overriding the model's NPC-weapon guess is *not* a "no silent substitution"
violation — that rule protects the player's stated intent. An NPC's weapon is engine-owned
statblock data, not the player's choice, so the model has no standing to pick it; the
entity whose choice matters still gets respected.



To be decided upon: 
────────────────────────────────────────────────────────────────────────
STEP 1 — consumables catalog + effect application (rules.py)
────────────────────────────────────────────────────────────────────────
- Add a CONSUMABLES table alongside WEAPONS/SPELLS/MONSTERS. The engine owns effects — the model
  must never invent what an item does. Start small:
    "healing_potion":  {"name": "Potion of Healing",  "effect": "heal", "dice": "2d4+2"}
    "greater_healing":  {"name": "Potion of Greater Healing", "effect": "heal", "dice": "4d4+4"}
    "pearl_of_power":  {"name": "Pearl of Power", "effect": "restore_slot", "level": 1}
- rules.apply_consumable(character, item_id) -> dict. Validates item_id in CONSUMABLES (ok=False
  "unknown_consumable" otherwise). Applies the effect through EXISTING engine paths:
    "heal":         roll the dice, then rules.heal(character, rolled.total) — rolled == applied,
                    same invariant as apply_dice; return rolled amount + resulting hp.
    "restore_slot": character.spell_slots[level] = character.spell_slots.get(level, 0) + 1;
                    return the new count.
  This function applies the EFFECT only — it does NOT touch inventory (the tool does that), the
  same way rules.attack/heal stay separate from dispatch.
- NOTE (your call, flag in DECISIONS.md): there is no max-spell-slot field, so restore_slot can't
  cap at a character's starting maximum. For rare single-use items a simple +1 is fine; if you'd
  rather cap it, add max_spell_slots to Character and clamp. Recommend uncapped +1 for now.


  ## ADR: Target agency is soft-enforced

**Status:** Accepted

**Context:** When a player attacks or casts without naming a target and more than one
living hostile is present, the player — not the engine or the model — should choose. But a
tool call erases provenance: the engine cannot tell a target the *player* named from one the
*model* guessed and filled in. There is no engine-side signal to validate against.

**Decision:** Ship target selection as a three-part tripod — schema (`defender`/`target`
optional), dispatch (a shared resolver that auto-resolves the sole living hostile and returns
`ok=false reason "ambiguous_target"` with a candidate list when several exist), and a
system-prompt rule (omit the target for *player* actions when unnamed with multiple hostiles;
NPCs always name a target, and the engine auto-picks for them). Candidates are the attacker's
*enemies* (a PC's are hostile NPCs; a hostile NPC's are the living party); the ambiguous→ask
path is PC-only.

**Consequences:** This is the project's one soft-enforced rule. The engine cannot detect a
violation — a model that fills in a valid target produces a tool call identical to a player
naming one — so the prompt rule is load-bearing, not belt-and-suspenders. Accepted as an
unenforceable residual; everything else (who is a legal target, single vs. multiple) stays hard.

## ADR: quest_flags hold narrative facts only (soft boundary)

**Status:** Accepted

**Context:** quest_flags is the durable cross-scene fact store. Two failure modes: the model
using it as a backdoor for mechanical state, and junk-drawering it with routine actions.

**Decision:** quest_flags records narrative/progress facts with no dedicated home — never
mechanical values. Two layers. Hard: a reserved-key denylist (`hp, max_hp, ac, spell_slots,
damage, initiative`) plus JSON-primitive value validation in dispatch. Soft: a prompt rule on
*what qualifies*, framed as action-vs-discovery — record a discovery, change, or commitment a
later turn must stay consistent with, never the act itself. Test: "would a later turn
contradict itself if this were forgotten?"

**Consequences:** The denylist stops obvious mechanical footguns but cannot catch a clever
proxy (e.g. a flag like `warden_favor` quietly used as a combat modifier) — that boundary is
prompt-only and unenforceable at the engine. What's flag-worthy is the model's judgment. The
rule was recalibrated once after observing over-flagging (`approached_snik`): the original
"flags are rare, when in doubt set none" suppressed legitimate discoveries, so it became the
action-vs-discovery discriminator, which keeps routine actions out without silencing real clues.

## ADR: Loot is author-placed and obvious-on-look

**Status:** Accepted

**Context:** Loot must be findable without the model inventing items, and without reveals
gated behind failable checks the model improvises — a bad roll on the run's only healing is a
feel-bad lockout.

**Decision:** Scene loot is author-placed (declared in the scene's `loot` list) and validated:
`take_item` grants only declared loot, so the model cannot conjure treasure (mirrors exits and
NPC templates). Present loot is obvious-on-look — the player choosing to search or examine is
the gate; the engine and model do NOT gate present loot behind `skill_check` or `roll_dice`. A
check applies only to content explicitly marked hidden. Generalized into a broad reveal
principle: never gate authored content (loot or written clues) behind an invented check.

**Consequences:** This replaced earlier behavior where the model invented failable checks to
gate reveals, locking the party out of load-bearing items on a low roll. The reveal gate is now
exploration *intent*, not a die roll. The "reveal through exploration" half is prompt-driven
(soft); the `take_item` validation that nothing un-declared can be granted is hard.

## ADR: Cross-scene resource economy — tight provisioning + loot, no rest

**Status:** Accepted

**Context:** Spell slots only deplete and never refresh, so casters run dry across scenes. A
recovery model was needed for a multi-scene run to function.

**Decision:** Tight starting provisions plus loot-as-recovery, and no formal mid-dungeon rest.
The party starts lean (the caster rations); recovery comes from found consumables — HP potions
and a rare slot-restorer. Using a consumable in combat costs that character's action.

**Consequences:** HP recovery and slot recovery are distinct problems; since the caster issue
is a *slot* problem, the slot-restoring item is load-bearing (HP potions alone don't solve it).
Chosen over formal rests (adds a mechanic and a rest-spam pacing problem, overkill for a 2–3
scene demo) and over pure provisioning (back-half grind). The choice is additive and
reversible: a rest layer can be added later — naturally at the session boundary for
multi-session play — without reworking loot; the cost of adding it later is rebalancing, not
code. The in-combat action cost is what keeps the valve from quietly undoing the tightness.

## ADR: Flag-gated transitions and endings

**Status:** Accepted

**Context:** quest_flags were recorded but gated nothing — `move_scene` validated only against
the scene graph, so a learned password or a held key could never actually block progress.

**Decision:** Exits and terminal endings may require a quest flag. An exit value is either a
bare `scene_key` (ungated) or `{to, requires, denied}`; a terminal scene may carry
`exit_requires`/`exit_denied`. `move_scene` rejects a gated way (`ok=false, reason "locked"`,
with the `denied` text) unless the required flag is truthy. A gated terminal scene defers its
victory to the gated move rather than auto-winning at combat-end; ungated terminal scenes
auto-win as before.

**Consequences:** Gating is on flag presence/truthiness, not value (value-matching via
`{flag, equals}` is a noted extension). Recording a flag and gating on it are now separate,
working capabilities. When a gate leads to a real scene (the iron door → iron_chamber), the
per-exit `requires` is the clean mechanism and the terminal `exit_requires` path goes unused;
the terminal gate exists for endings with no scene beyond. Backward compatible: string exits
and ungated terminals are unchanged.

- **What:** String-valued quest flags (including answer-gate passwords stored via 
  `set_quest_flag`) are now redacted to `"<redacted>"` in both the `get_state` response 
  and the `set_quest_flag` echo before they reach the model. The real value is stored in 
  `state.quest_flags` and is used by the answer-gate engine logic in `move_scene`.
- **Why:** The `get_state` snapshot is injected into the model's context every turn. A 
  string-valued flag like `{"iron_door_password": "ashfall"}` was visible to the model, 
  meaning it could supply the password itself rather than relaying the player's words — 
  defeating the soft boundary documented in the README.
- **What this hardens:** The `get_state` channel and the `set_quest_flag` echo channel. 
  The `_exits_for_model` helper already redacted `requires_answer` from exits; this 
  extends the same principle to quest flags.
- **What it doesn't fix:** A scenario author who stores a password in a boolean flag (e.g. 
  `"ashfall_known": true`) rather than a string flag still leaks the flag name. That's 
  an author-convention problem, not addressable in code without a flag metadata schema.
- **Classification:** Hard boundary (enforced in code), not soft boundary.


## ADR: add_npc spawns only author-declared reinforcements, behind a trigger

**Status:** Accepted

**Context:** `add_npc` originally let the model spawn any monster from the `MONSTERS`
table at will — stats were engine-owned, but *whether*, *when*, *how many*, and *which*
were pure model discretion. That made it the one tool that let the model author world
*content*, not just propose actions, breaking the discipline its siblings enforce:
`move_scene` rejects undeclared exits, `take_item` rejects unlisted loot — the author
declares the possibility-space and the engine holds the model inside it. `add_npc`
checked against nothing but the template name. (It also accepted an empty `instance_id`,
silently creating a blank-string roster key.)

**Decision.** Reinforcements are author-placed, mirroring loot and exits. A scene declares
a `reinforcements` manifest keyed by `instance_id`, each entry an NPC spec
(`{template, name, …overrides}`) expanded through the same `expand_npc_entry` path as scene
NPCs. `add_npc` takes only `instance_id` and may spawn only a declared key — stats, name,
and template come from the manifest, never the model. An entry may carry a `requires` flag:
the **authored trigger**. A gated reinforcement is hidden from the model's state snapshot
(and `get_state`) and refused on spawn (`ok=false reason "locked"`) until that flag is set;
the flag fires in the fiction via `set_quest_flag`, exactly like a gated exit opening. Each
spawns once (`already_spawned`); empty ids are rejected (`missing_instance_id`).

**Consequences.** Two separations now hold where one did before: stats were always
engine-owned (numbers), and now *existence* is author-owned (content). What the model still
owns is the *timing within the authored gate* — it decides the dramatic moment to bring in
a triggered wave, and it sets the trigger flag, so the gate is hard on **what/whether** but
soft on **when** (same trust model as gated exits and quest flags generally). The feature
ships **dormant**: no scenario declares a manifest yet, so `add_npc` succeeds nowhere until
an author opts in — turning it on is a scene-authoring act, not a code change. The neat
mid-combat initiative-insertion capability (sorted-slot placement, pointer-stable) is
preserved unchanged; only the source of *what* may enter was constrained. Backward note:
the old free-form `template`/`name` arguments are gone from the schema.

KNOWN ROUGH EDGE (leave for now): a de-escalated NPC stays in combat_order if other hostiles
remain, so its turn still comes up — the system-prompt rule above handles it (narrate it standing
aside). Cleanly removing it from the order is a later refinement; do not modify next_turn here.
