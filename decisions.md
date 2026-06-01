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

---

## 5. Fold narration into the tool loop; one narration call per turn

**Decision.** Narration is no longer a dedicated second model call per action.
Instead:

- **Out of combat**, the tool loop's *terminating turn* IS the narration. The
  model resolves the action with tools, then writes its final (non-tool) message
  as the in-world prose. `_execute(capture_narration=True)` returns that text. The
  call that used to be spent generating a throwaway terminator now does the work.
- **In combat**, the player's action is resolved tool-only, the engine runs the
  following NPC beats (engine-resolved, no API call), and **one** unified call
  (`_narrate_turn`) narrates the player's action *and* every NPC beat in order.
- **Combat-over** and **end-of-run** keep dedicated calls (`_narrate_combat_over`,
  `_narrate_epilogue`): their prose is shaped differently and is only known after
  the action resolves.

This supersedes the player-action half of **Decision 2**: the player beat is no
longer on its own dedicated call. Decision 2's batching of NPC beats into one call
stands — it is now the same call that carries the player beat.

**Why.** Profiling a full run (see the per-call stats sidecar) showed the dedicated
narration phase was ~25 calls / ~92s / ~28.6k uncached input across 26 turns, *on
top of* a terminating tool-loop generation that was scrubbed and thrown away. The
two together were the dominant, serial per-turn cost. Merging removes the dedicated
player-narration call (out of combat) and collapses player + NPC narration into one
call (in combat): ~3.5 API calls/turn → ~2 out of combat, ~3 in combat.

**Trade-off — the leak guards become load-bearing.** The two-phase split previously
gave defense-in-depth against the model dumping raw state/JSON into prose: a clean
"now just narrate" reframe *and* the `_extract_narration` / `_sanitize_narration`
screens. Producing prose in the same breath as tool reasoning, with tool JSON fresh
in context, raises the odds of a leaked dump — so those two screens go from
belt-and-suspenders to the **primary** defense. A regex regression there is now
directly player-facing. We accept this: the screens are unit-tested
(`test_narration_leak_regression`, plus the `_extract`/`_sanitize` suites), the
engine still owns every number regardless of prose, and the latency win is large.

**Trade-off — NPC-beat coverage.** Same as Decision 2: folding beats into one call
means the model could drop or merge a beat. The enumerated, numbered beat list in
`_narrate_turn` mitigates it; engine state stays authoritative, so the worst case is
incomplete *prose*, never wrong *numbers*.

**Scope — what's unaffected.** Purely how prose is produced. Tool dispatch,
enforcement, turn order, redaction, and the engine's authority over numbers are
untouched. The terminating generation in combat turns is still spent (the API can't
signal "done with tools" without a final response) — eliminating that too would mean
injecting NPC beats mid-`_execute` so its terminator narrates everything; left as a
future refinement.

**Reversibility.** Localized to `dm_agent.py` (`_execute`, `_narrate_turn`,
`take_turn`) and the system-prompt protocol block. Reverting to a dedicated
narration call is mechanical.

---

## 6. Stream narration to the terminal, behind a leak gate

**Decision.** The DM agent exposes an optional `on_narration_delta` sink. When set
(the terminal REPL sets it), the dedicated text-only narration calls — `_narrate_turn`,
`_narrate_combat_over`, `_narrate_epilogue` — stream their prose via
`client.messages.stream` and push tokens to the sink as they arrive, instead of
returning the whole paragraph after a blocking `create`. When the sink is unset
(library callers, the no-API test suite), the buffered `create` path runs unchanged,
so behavior and mocks are identical to before.

The out-of-combat captured narration (the tool loop's terminating turn, Decision 5)
is **not** streamed: it is produced inside `_execute`, where a response isn't known
to be the terminal (narration) turn until the stream completes — so there is nothing
to safely stream live. It stays buffered + screened and is emitted to the sink as one
chunk. Streaming covers every combat turn and the epilogue — the longest narrations
in profiling (the multi-beat combat narration and the closing beats), where perceived
latency matters most.

**Why.** Profiling showed narration is decode-bound (~30 tok/s) and the largest
single contributor to per-turn wall time. Streaming doesn't reduce tokens or total
generation time, but it cuts *perceived* latency sharply: the player starts reading
at the first token instead of waiting for the whole paragraph.

**Trade-off — the leak gate.** Decision 5 already made the leak screens load-bearing.
Streaming raw tokens would defeat them: a leaked `[Current state]…{…}` dump would hit
the screen before any post-hoc screen could suppress it. So streamed deltas pass
through a `_NarrationGate`: it holds the *opening* of the stream until it can rule out
a leading dump (the observed leak shape is always dump-first). Plain prose (not
starting with `[`/`{`/`"party"`) is released immediately and then passed through live;
bracket/brace-leading text is held until a paragraph boundary lets the same screens
(`_screen_narration_text` + `_sanitize_narration`) decide what survives. The realistic
dump-first leak never reaches the sink, and on-screen output equals what is stored. The
residual gap — clean prose *followed* by a mid-stream dump — is the same pathological
case `_sanitize_narration` was always weak on, and is not the observed failure mode.

**Scope — what's unaffected.** Pure presentation/transport. No tool, enforcement,
redaction, or storage change; `take_turn` still returns the full assembled narration
for history and logging, and the terminal simply stops re-printing it (the sink already
showed it). The mechanical tool-selection-on-a-faster-model idea (README roadmap)
remains open and independent.

**Reversibility.** Leaving `on_narration_delta` unset reverts to fully buffered
narration with no other change. Localized to `dm_agent.py` (`_narration_call`,
`_NarrationGate`) and the REPL wiring in `main.py`.

---

## 7. Companions: recruiting a cross-scene ally

**Decision.** A non-hostile NPC can be recruited with `recruit_npc` (between fights, not
mid-combat); it sets a `companion` flag on the NPC, which then follows the party across
scenes and fights hostiles on the party's side.

- *Recruit gating.* Out-of-combat only, and the NPC must already be non-hostile
  (de-escalate via `influence_npc` first). Mid-combat recruiting would require
  inserting a new actor into the live initiative order — deferred as not worth it.
- *Cross-scene follow.* `move_scene` rebuilds `state.npcs` from the destination
  scene; companions are carried forward (re-keyed on collision) so they travel with
  the party. Their surprise flag is cleared — a new scene isn't a surprise round.
- *Combat.* `resolve_npc_action` resolves a companion's turn engine-side: it attacks
  the lowest-HP living hostile, mirroring how a plain hostile attacks the lowest-HP PC.
  The model includes companions in `start_combat`; their beats narrate like any NPC's.

**Deliberate simplifications.** Hostiles still target **PCs only** — they ignore
companions. And a full party wipe is still a **defeat even if a companion survives**: a
companion augments the party, it doesn't substitute for it. Both keep the win/lose
condition anchored on the player characters and avoid an ally-only victory/stalemate.
Revisit if companions become central rather than a bonus.

**Scope — unaffected.** Enforcement, turn order, and redaction are untouched; the new
`companion` field serializes through the existing `asdict`/`from_dict` path.

## ADR: Concluding an empty terminal scene (soft trigger, hard gate)

**Status:** Accepted

**Context:** A terminal scene (empty `exits`) that contains hostiles wins
deterministically via the combat-clear path (`_adjudicate_combat_outcome` sets
victory when the last hostile drops in an exitless scene). But a *hostile-free*
terminal scene — an exploration/puzzle ending such as the vault beyond the iron
door, a relic chamber — had no reliable victory trigger. The only engine path was
the player calling `move_scene` with a non-exit key, which the system prompt forbade
("empty exits = dead end, nowhere to go"). Observed in a real run
(`saves/epilogue_not_firing_stats_trace.json`): the party spoke the password, entered
the empty terminal vault, looted it, and tried to "leave"/"exit" twice — the model
never called `move_scene`, so `game_over` was never set and the epilogue never fired;
the run hung in the winning room.

**Decision.** Conclude a hostile-free terminal scene via a **soft trigger** and a
**hard gate**.
- *Soft (prompt):* when the player signals leaving/finishing in a terminal scene with
  no living hostiles, the model calls `move_scene` with the current scene's own key.
- *Hard (engine):* `move_scene` from an exitless scene grants victory only when no
  living hostile remains (new `hostiles_present` refusal otherwise); the engine —
  never the model — sets `game_over`/`game_outcome` and fires the epilogue. The model
  is explicitly forbidden from writing an ending without that `move_scene` call.

**Considered and rejected.** (a) *Instant victory on entering* a hostile-free terminal
scene — robust and deterministic, but cuts off looting/roleplay in the final room (the
reward becomes epilogue-only, or the loot must move one scene back). (b) *Require every
terminal scene to contain a hostile* — works through the existing combat path with no
code, but forces a fight onto exploration/puzzle endings, against their intent.

**Consequences.** This is a soft boundary, like target-agency and password-relay: the
engine cannot make the model recognize "the player wants to leave," so the *trigger* is
model-dependent and not unit-testable end to end (the trace shows the model failing it
once — the accepted risk). The hard half bounds the failure modes: the model cannot
declare victory itself (`game_over` is engine-owned), and cannot conclude while a foe
still stands (`hostiles_present`). The worst case is a missed or one-turn-early
conclusion of the *final* room — never a fabricated mid-dungeon ending or wrong numbers.
The engine gate (victory only when hostile-free; refusal otherwise) is hard-tested;
only the model's leave/finish recognition is soft.

## ADR: Hazards & traps are author-placed; the engine owns the numbers

**Status:** Accepted

**Context:** Saving throws (`saving_throw`) gave the engine a reactive d20-vs-DC roll, but
nothing *used* it as authored scene content. The stopgap (see `demo_saving_throws`) wrote
the trap's ability and DC into the scene prose so the model would read them and pass them
to `saving_throw` + `apply_dice` — which means the model held the trap's numbers and could,
in principle, fudge the DC or damage, or invent a trap wholesale. That breaks the discipline
every other piece of authored content already enforces (`move_scene` rejects undeclared
exits, `take_item` rejects unlisted loot, `add_npc` rejects undeclared reinforcements).

**Decision.** A scene declares a `hazards` manifest keyed by hazard id, each entry an
author spec: `{name, ability, dc, damage, damage_type, on_success ("none"|"half"), once,
requires, hidden}`. A new `trigger_hazard(hazard_id, characters?)` tool springs one. The
manifest is the sole authority — the model may only trigger a declared id, **never** supplies
the ability/DC/damage, and the engine resolves the whole thing atomically: roll the damage
once (shared across an area), roll each affected character's save (via `saving_throw`, so it
is proficiency-aware), and apply full / half / no damage through `apply_damage`. The model
chooses only the *fiction* — which hazard fires, when, and who is caught (omit `characters`
to hit all conscious party members). Hazards reuse the reinforcement gating idioms: a
`requires` flag keeps a hazard disarmed and hidden until set; a one-shot (`once`, default
true) fires once and is recorded in `GameState.sprung_hazards` (`"<scene>:<id>"`, serialized);
`hidden` marks a concealed trap the prompt forbids telegraphing before it springs.

**Consequences.** Two boundaries now hold where the stopgap held none: *existence* is
author-owned (no invented traps) and *numbers* are engine-owned (DC and damage never reach
the model — the snapshot surfaces only id/name/hidden). What the model still owns is the
*timing and targeting within the fiction* — soft, like reveal-through-exploration for loot.
Scope kept deliberately tight for v1: hazards resolve a single *saving throw* and deal
*dice damage* (or none/half); a detect/disarm *check*, condition effects (frighten, poison
as a status), and recurring environmental ticks are left as extensions. `trigger_hazard` is
not turn-guarded — a trap is environmental, not an actor's action, so it may fire on anyone's
turn. The hard parts (declared-only, gating, once, save+damage math) are unit-tested; only
the model's choice of when to spring a hazard is soft. `demo_saving_throws` is converted from
the prose-DC stopgap to a real manifest, retaining one bare `saving_throw` (the fear ward) to
show the distinction: not every save is a hazard.
