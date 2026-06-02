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

**Structure.** The entries below are grouped: *Architectural decisions* — choices between alternatives with system-wide significance — followed by a short *Enforcement invariants & fix notes* section for entries written in ADR form that are really durable invariants or regression fixes (no architectural fork was weighed). The latter live here, beside their tests, rather than in the user-facing README.

---

# Architectural decisions

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
(the first weapon from the `WEAPONS` table in its inventory, melee or ranged); the
model names only attacker and defender, never the weapon. PC attacks are unchanged —
the player names the weapon, validated against inventory.

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

## ADR: String-valued quest flags are redacted from model-facing channels

**Status:** Accepted

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

## ADR: get_state hides the hidden NPC challenge DCs

**Status:** Accepted

**Context:** The per-turn injected snapshot (`dm_agent._state_snapshot`) deliberately omits an
NPC's `alertness_dc` (surfacing only `surprised`) and never carries `disposition_dc` — the
README promises "hidden stealth DCs" are redacted from "everything the model sees." But the
`get_state` tool returned `state.to_dict()` with light redaction (quest flags, exits, history)
and **left the NPC dicts raw**, so a model that explicitly called `get_state` received both
`alertness_dc` and `disposition_dc` in full. The auto-snapshot was airtight; the on-demand
channel was not — the same leak class the quest-flag ADR above closed for passwords.

**Decision:** `get_state` runs its NPC map through `_npcs_for_model`, dropping
`_HIDDEN_NPC_FIELDS` (`disposition_dc`, `alertness_dc`) before the result reaches the model.
`alertness_dc` is the stealth DC the README names; `disposition_dc` is its one-shot social
twin. The model is meant to learn an NPC's reachability by *attempting* — `influence_npc`
returns `immovable` for a `None` disposition, `attempt_ambush` returns `cannot_ambush` or the
`bar` — never by reading the number off the snapshot. The live `state.npcs` objects keep both
(the engine owns and rolls against them); only the model-facing copy is stripped.

**Consequences:** `get_state` now matches `_state_snapshot`'s NPC view for the two secret DCs,
so the README's redaction claim holds on both channels. Other NPC fields (`hp`, `hostile`,
`ac`, `attack_bonus`, `inventory`, `social_attempted`, `surprised`, `companion`) still surface
— they are not secrets (AC/atk are revealed through combat and shown in `/state`). Hard
boundary, enforced in code (`test_get_state_hides_hidden_npc_dcs`). Saves are unaffected:
`to_dict`/`from_dict` still round-trip both DCs (the redaction is applied only in the
`get_state` dispatch, not in serialization).

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
soft on **when** (same trust model as gated exits and quest flags generally). Authors have
since opted in: `data/adventures/tomb_of_the_sunken_king.json` declares a `reinforcements`
manifest (the risen court, gated on `crypt_disturbed`) and `data/demos/demo_reinforcements.json`
exercises it — so the feature is live in shipped content, not dormant; turning it on for a
new scene is a scene-authoring act, not a code change. The neat
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
call (in combat): ~3.5 API calls/turn → ~2 out of combat, ~3 in combat. (A later change cut
the count further — see *ADR: Cut two avoidable API hops per turn*.)

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
dump-first leak never reaches the sink.

**Accepted residual — the streaming on-screen path (decided, not a TODO).** The gap is
now isolated to *live streaming*: once the gate has released leading prose it flips to
live pass-through, so a model that writes clean prose and *then* dumps mid-stream would
flash that dump on screen before the stream ends. We accept this deliberately. Closing
it would mean re-screening every post-opening delta — or buffering the whole stream and
re-checking — which defeats the live, token-by-token streaming this decision exists to
provide; the trade we are making is transient on-screen exposure of a pathological,
unobserved ordering in exchange for the streaming UX. It is backstopped on the side that
matters most: the *stored* value is re-screened by `_sanitize_narration` in `take_turn`,
which — since the dump-anywhere fix (it now drops a dump paragraph wherever it sits, not
only when dump-first) — keeps `state.narrative`/`transcript` and the `take_turn` return
clean even in this ordering. So only the transient render could flicker; persisted state
and the dump-first case (which the gate fully suppresses) stay airtight. Revisit if a
real prose-then-dump leak is ever observed, or if the gate moves to a fully buffered
screen-then-stream model.

**Scope — what's unaffected.** Pure presentation/transport. No tool, enforcement,
redaction, or storage change; `take_turn` still returns the full assembled narration
for history and logging, and the terminal simply stops re-printing it (the sink already
showed it). The mechanical tool-selection-on-a-faster-model idea is independent and has
since been implemented — see *ADR: Two-model split*.

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
(captured in a since-cleared trace): the party spoke the password, entered
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
show the distinction: not every save is a hazard. Hazard manifests now also ship in shipped
adventures (`data/adventures/emberdeep_mine.json`, `tomb_of_the_sunken_king.json`).

## ADR: Underspecified social intent — ask, never default the actor/approach

**Status:** Accepted

**Context:** `influence_npc` requires three things the model supplies by name — the acting party
member, the target NPC, and the approach (`persuade`/`intimidate`) — and the engine auto-resolves
none of them. That is asymmetric with target selection: an omitted *target* on `attack`/`cast_spell`
is auto-resolved or bounced back as `ambiguous_target` (a hard engine seam backstopping a soft
prompt rule). There is no equivalent for the *actor* or the *approach* — every action tool requires
the acting character, so the engine literally cannot represent "the player didn't say who," and out
of combat there is no active combatant to imply one. A vague social input ("speak to the goblin")
names neither the actor nor the approach, and the SOCIAL prompt rule only modeled the act branch
("call `influence_npc` with the approach"). Two saved stats-traces from the identical opening
(since cleared) captured the result: on the same input and
byte-identical state, one run guessed (Aldric + persuade) and fired `influence_npc`; the other
paused and asked which character. Same prompt, same context — pure model sampling, because nothing
deterministic chose between guess-and-fire and ask.

**Decision:** Make the soft rule explicit and one-directional. When social intent is expressed
without a *named acting character* AND an *explicit approach*, the model STOPS and asks — names the
present party members, asks persuade-or-intimidate — emitting no action tool that turn, mirroring
the `ambiguous_target` re-prompt. It never defaults the actor or the approach, and must not assume
a bare "speak to it" is even a Charisma attempt. If only one of the two is missing it asks only for
the gap; a fully specified attempt ("Aldric intimidates the goblin") resolves immediately. In
combat the actor is fixed by turn order (the active combatant), so only the approach can be missing
there. Lives in the SOCIAL block of `SYSTEM_PROMPT`.

**Consequences:** A **soft boundary**, in the same family as target-agency's prompt half and
password-relay — the engine still has no `ambiguous_actor` seam, so this *reduces* but cannot
eliminate the guess-path (a model that names a plausible actor produces a tool call indistinguishable
from a player-named one). It deliberately costs a clarification round-trip on vague social inputs —
the intended "always ask" behavior, chosen over defaulting because the acting character is the
player's agency, not the model's. Hardening would need an engine affordance the action tools don't
have today — an omittable actor with an `ambiguous_actor` resolver, the social-side mirror of
`_resolve_offensive_target` — recorded as a possible future hard backstop, not built. Scope is
`influence_npc`; other unnamed-actor cases are out of scope (in combat the turn-guard already fixes
the actor).

## ADR: Inspiration is an engine-owned reroll budget; the award is a safe soft boundary

**Status:** Accepted

**Context:** The roadmap's easiest next mechanic is a "once-per-session reroll the DM can award"
(inspiration / luck point). The concern: "the DM awards it" hands the *model* a lever over a game
outcome, which looks like it might breach the project's core discipline (every game number is
engine-owned; the model only requests). The question is whether inspiration can be added without
letting narration launder a better result.

**Decision.** Split the mechanic into its two halves and place each on the correct side of the
boundary. (1) The **reroll and the budget are hard, engine-owned**: a PC carries `inspiration`
(0/1) and `inspiration_used` (a lifetime lock) on `Character`; `rules.award_inspiration` caps the
hold at one and refuses re-award after a spend (`already_used`) — the same refuse-when-spent shape
as `cast_spell`'s slot economy and the Pearl-of-Power cap. Spending is a flag (`use_inspiration`)
on `skill_check` / `saving_throw`; when set and a point is held, the engine rolls 2d20-keep-higher
*inside the same atomic roll*, zeroes the point, and locks it. The reroll goes through `roll("1d20")`
so `--seed`/`force_rolls` still drive it. (2) The **award decision is soft** — a DM judgment about
clever play or roleplay, held in `SYSTEM_PROMPT`, in the same family as target-agency and
reveal-through-exploration.

**Consequences.** The soft half is *benign* because the engine still owns the cap and the dice: the
model's discretion affects only *whether* a reroll is offered, never *what number* comes up, and it
can never grant a second one or pick the result. Scope kept tight: spend applies to the two clean
d20 rolls (`skill_check`, `saving_throw`) that have no atomic damage side-effect; threading it
through `attack` / spell-attack (which would touch the crit/nat-20 path) is left as a same-flag
extension. Semantics are advantage-style (2d20 keep higher) rather than a true retroactive reroll,
which avoids having to store and undo a committed roll. Economy chosen: one reroll per character for
the entire session (lifetime lock), the strictest faithful reading of "once-per-session." The hard
parts (cap, lifetime lock, advantage roll, decrement) are unit-tested; only *when to award* is soft.

## ADR: Cut two avoidable API hops per turn (latency)

**Status:** Accepted

**Context:** Trace analysis of 40 demo stats sidecars (305 turns, 742 calls) showed 2.43 API
calls/turn against a ~3.3s fixed per-call cost, with 34% of turns paying a second
tool-selection hop. Two causes dominated.

**Decision.** (1) *Redundant `get_state` (~20% of turns):* the per-turn `[Current state]`
snapshot already carries HP/slots/conditions/inventory/spells/NPCs/scene/exits/loot/flags/
combat order, yet the system prompt still said "call `get_state` rather than guessing" — a
line predating snapshot injection. Reworded to act on the snapshot directly and reserve
`get_state` for a rare omitted detail. (2) *Wasted terminal hop after a failed combat action
(44 turns, ~188s):* the combat fast-path broke only on `action_used` (success); an `ok=false`
rejection (turn-guard, `ambiguous_target`, `combat_starting`, …) left `action_used` False, so
the loop spent one more terminal `create()` whose text is scrubbed in combat anyway. Now also
breaks on a hard-stop `ok=false` in the same hop.

**Consequences.** This supersedes Decision 5's call-count figures (which measured the
narration-folding win before this cut). Worst case is unchanged — the engine still owns every
number; only redundant generations were removed. A test asserts a rejected combat action now
yields exactly one thinking call (verified failing at two without the fix). Commit `3ad6b13`.
(The separately-attempted parallel `tool_use` batching — `bf7cac0` and its "§8" ADR — was
reverted in `dce6695`/`9a4ff2b` and is intentionally not documented.)

## ADR: Gold ledger and merchants (buy/sell)

**Status:** Accepted

**Context:** The "Cross-scene resource economy" ADR established provisioning + loot + no rest,
but had no *trade* layer — found gear and gold could not be exchanged. Gold needed an
owner-enforced home for the same reason every other game number does.

**Decision.** A per-character purse — `Character.gold` (int) — mutated only through
`rules.add_gold` / `rules.spend_gold`, where `spend_gold` refuses an overspend
(`insufficient_gold`) without changing the balance, the same refuse-when-short shape as the
spell-slot economy. Merchant NPCs carry an authored `NPC.shop` catalogue (`{item_id:
price_gp}`); a non-empty `shop` is what marks an NPC a merchant. Two tools, `buy_item` and
`sell_item` (`src/tools.py`), take the acting party member and item (merchant optional —
auto-selects the sole shopkeeper present). The engine owns every number: a buy pays the
catalogue price and refuses `not_for_sale` / `insufficient_gold`; a sell credits **half**
catalogue price (`SELL_RATE`) and refuses `not_in_inventory` / `not_buying` — a merchant only
buys back what it stocks. Stock is an infinite catalogue (a buy never depletes it); the
buyer/seller must be a PC (`not_a_pc`).

**Consequences.** Engine-hard: prices, balance math, the half-price buyback, and every
refusal. Soft (the model's): the *fiction* of haggling/roleplay around a sale, and choosing
when to trade. The author owns the catalogue and its prices (declared in the scene's NPC
`shop`), so the model can neither invent stock nor set prices — mirroring loot (`take_item`)
and reinforcements (`add_npc`). v1 scope: flat infinite stock, fixed prices (no dynamic
pricing or haggle mechanic), single-unit transactions. Commits `a4658ad` (ledger), `b8922c1`
(merchants).

## ADR: Two-model split — tool-selection on a fast model (latency)

**Status:** Accepted

**Context:** After the call-count cuts (Decisions 2/3/5/6 + *Cut two avoidable API hops*), a
profiled run (`saves/ember_deep_run_1_stats_trace.json`, 48 turns) sat at **2.02 API
calls/turn** — so the remaining lever is per-call speed, not call count. Tool-selection
("thinking") was **~35% of wall** (121.5s) doing mechanical, low-output (~83 tok) work — a
fast/cheap model's job — while narration (65%) is decode-bound and quality-sensitive. This is
the README roadmap's rank-#1 "two-model split."

**Decision.** Two model constants in `src/dm_agent.py`: `MODEL` (quality, narration) and
`FAST_MODEL` (Haiku, tool-selection). `_execute` picks the model **once per call** by its
`capture_narration` flag: `capture_narration=False` (the combat player-action and batched
NPC-fallback loops, whose terminating text is scrubbed) runs on `FAST_MODEL`; the folded
out-of-combat loop (`capture_narration=True`, whose terminating turn *is* the narration —
Decision 5) and every `_narration_call` stay on `MODEL`. This is the key constraint: the fold
means out-of-combat thinking can't move to the fast model without re-introducing a separate
narration call, so only the scrubbed-prose loops are routed. Each `api_stats` entry is tagged
with the model that served it; `views.estimate_cost_mixed` prices a mixed-model session
per-call. `DMAgent(fast_model=None)` disables the split (everything on `MODEL`).

**Why it's safe.** Zero prose-quality risk: the routed calls never produce player-facing text
(it's scrubbed; narration is a later `MODEL` call). The hard boundaries are untouched — the
engine still refuses illegal actions and `MODEL` still writes every line, including
enforced-failure prose, so a wrong fast-model arg-pick surfaces as an `ok=false` the quality
model narrates, never as a wrong number or a leak.

**Consequences.** Validated on `saves/ember_deep_run_2_stats_trace.json`: routing exactly as
designed (40 combat thinking calls on Haiku at **1.54s** vs the 2.53s Sonnet baseline; 9
out-of-combat thinking calls and all 47 narration calls on Sonnet), **thinking-bucket wall
−29%**, and **zero** bad tool picks (the only two `ok=false` were legitimate enforcement),
outcome victory. $ saving is modest (thinking input is cache-read-dominated, output tiny) —
this is a latency lever. **Reversibility:** `fast_model=None` reverts instantly. Commit
`b297f08`.

## Ranking of ADR significance

Ordered most → least significant. The criterion is how foundational the decision is to the
project's central thesis — *every game number is owned and enforced by code; the model only
narrates* — and how much of the system depends on it. Enforcement boundaries and core loop
architecture rank above feature mechanics, which rank above localized optimizations and
narrow correctness rules.

1. **Target agency is soft-enforced** — the clearest articulation of the hard-vs-soft
   boundary doctrine that defines the whole project; the template every other "soft boundary,
   documented as such" decision follows.
2. **#3 Bound the per-turn model context** — the architectural keystone: a fresh, bounded
   context each turn (not append-only) keeps per-turn cost flat and makes the loop tractable;
   nearly every later decision assumes it.
3. **quest_flags hold narrative facts only** — guards the one general-purpose state-write the
   model has, with a hard reserved-key denylist so flags can't become a backdoor to
   mechanical state.
4. **#5 Fold narration into the tool loop** — restructured how every turn produces prose and
   promoted the leak screens from belt-and-suspenders to the *primary* defense.
5. **#2 Batch NPC turn narration into a single model call** — established the one-call
   per-turn-exchange shape that #5 and the combat loop build on.
6. **String-valued quest flags are redacted from model-facing channels** — keeps passwords
   and other secrets out of model context; load-bearing for the entire answer-gate mechanic.
7. **get_state hides the hidden NPC challenge DCs** — keeps stealth/social DCs out of the
   model's hands so reachability is learned by *attempting*, not reading a number.
8. **Loot is author-placed and obvious-on-look** — the model cannot fabricate treasure; first
   pillar of the "author owns content, model only triggers" family.
9. **add_npc spawns only author-declared reinforcements, behind a trigger** — the model cannot
   conjure monsters; the encounter roster is author-owned.
10. **Hazards & traps are author-placed; the engine owns the numbers** — traps' save/DC/damage
    never leave the engine; the author-placed twin of `saving_throw`.
11. **Flag-gated transitions and endings** — the engine owns passage through the map and when
    a gated ending opens; the model can't fabricate a route.
12. **Cross-scene resource economy — tight provisioning + loot, no rest** — the design frame
    that makes resources (slots, HP, items) actually matter across a run.
13. **Concluding an empty terminal scene (soft trigger, hard gate)** — who decides the run is
    over: a soft model trigger backed by a hard engine gate.
14. **Companions: recruiting a cross-scene ally** — a whole cross-scene ally subsystem that
    fights engine-resolved on the party's side.
15. **Gold ledger and merchants (buy/sell)** — the economy layer; engine-owned prices and
    purse, mirroring the loot/reinforcement authority model.
16. **Inspiration is an engine-owned reroll budget; the award is a safe soft boundary** — a
    capped reroll resource; the canonical "safe soft boundary" (the judgment is soft, the
    budget and dice are hard).
17. **Underspecified social intent — ask, never default the actor/approach** — the
    clarify-don't-guess policy generalized from `ambiguous_target`.
18. **#6 Stream narration to the terminal, behind a leak gate** — perceived-latency win plus
    the streaming leak gate that keeps the screens honest under live output.
19. **Cut two avoidable API hops per turn (latency)** — removed a redundant `get_state` and a
    wasted terminal combat hop; a measured call-count win.
20. **Two-model split — tool-selection on a fast model (latency)** — routes mechanical
    tool-selection to a fast model, a per-call latency win with no enforcement risk.
21. **#4 NPC weapons are engine-selected, not model-named** — keeps weapon dice/numbers
    engine-owned for NPC attacks.
22. **#1 Out-of-turn declared actions — discard and acknowledge** — a turn-integrity rule for
    a player declaring an action when it isn't their turn.

The two entries under *Enforcement invariants & fix notes* below — the leveled-spell base-level rule and the turn-prompt strip — are intentionally excluded from this ranking; they are durable invariants / regression fixes, not decisions between architectural alternatives.

# Enforcement invariants & fix notes

## Invariant: A leveled spell cannot be cast below its tabled base level

**Status:** Accepted

**Context:** `cast_spell` treats *any* cast at `spell_level == 0` as a free cantrip
(`slots_remaining: "n/a"`, no slot charged) — it never checked that the *named* spell is actually a
cantrip. Separately, `cast_damaging_spell` resolved damage with
`by_slot.get(spell_level, by_slot[max(by_slot)])`, falling back to the **strongest** tabled version
when the level wasn't found. Together these opened a slot-economy bypass: a caster out of slots could
cast a leveled spell — e.g. `magic_missile` (base level 1) — by declaring `spell_level: 0`. The
engine charged no slot *and* auto-picked the highest upcast (`by_slot[3]`, `5d4+5`). A live 5-scene
trace caught exactly this on turn 27: a Wisp at `L1 (0/3)`, holding only an `L2 (1/1)` slot, fired
Magic Missile for free at the L3 damage column; the persisted save confirmed the L2 slot was never
touched. This is the same invariant `test_spell_slots_run_out` exists to protect (the engine owns the
slot economy; the model cannot narrate around a refusal), reached through a side door.

**Decision:** `cast_damaging_spell` looks up the spell's tabled entry **before** consuming anything
and refuses a cast whose `spell_level` is below the spell's base `level`
(`ok=false, reason "…is a level-N spell and cannot be cast with a level-M slot", below_min_level=True`).
The check sits between the knowledge check (a) and slot consumption (b), so no slot — of any level —
is spent on a refused cast. With under-leveling blocked, the `by_slot[max(...)]` fallback is now only
reachable by *over*-leveling (upcasting above the tabled max), where capping at the strongest tabled
column is the intended behavior. Cantrips (base level 0) are unaffected: `0 < 0` is false, so they
keep the free path.

**Consequences:** The model must now actually spend the right slot — to finish a foe with Magic
Missile when out of L1 slots, it must upcast into the held L2 slot (`spell_level: 2` → `4d4+4`), or
narrate the fizzle. Hard boundary, enforced in code
(`test_cast_leveled_spell_below_base_level_refused`, `test_cast_leveled_spell_upcast_above_base_still_allowed`).
Untabled spells (no `SPELLS` entry) are not level-checked — the engine can't know their base level —
so they retain the prior slot-consumed-and-narrated behavior. Related, same trace: `heal` now reports
`healed` as the HP *actually* restored after the max-HP cap (`target.hp - hp_before`) rather than the
raw roll, so a capped potion no longer hands the model an inflated number to narrate.

## Fix note: Strip model-written turn prompts that duplicate the engine's

**Status:** Accepted

**Context:** The engine owns the per-turn combat prompt (`DMAgent._closing_prompt` in
`src/dm_agent.py`), appended deterministically as a trailer after each turn's narration. But
the model frequently *also* ended its prose with one — e.g. `**Brom, what do you do?**`. A
logged run (`saves/ember_deep_run_1.json`) showed this in **29 of 48** beats. Two harms: the
player saw a duplicate (the model's bolded prompt immediately followed by the engine's
trailer), and the contaminated narration was persisted to `narration_history` /
`state.narrative`, which feed the bounded per-turn window (Decision 3) — so the model read
its own prompts back as in-context examples and imitated them, a self-reinforcing leak.

**Decision.** `_strip_turn_prompt(text, names)` removes a trailing prompt addressed to a
known actor **by name** (`**Tilda, you're up — what do you do?**`, `**Sage**, what do you
do?`), wired into all three narration-assembly sites in `take_turn`. It is name-anchored so a
*deliberate* party-wide exploration prompt from the combat-over close ("… What do you do?",
which names no one) and a rhetorical line ("Will Brom survive?", no comma) are preserved.

**Update (broadened regex, commit `b297f08`).** The original regex required the name be
**immediately followed by a comma**, so it caught only the whole-sentence-bold shape
(`**Sage, …?**`). A later logged run (`saves/ember_deep_run_2.json`) showed the narrator had
drifted to **name-only bold** (`**Sage**, what do you do?`) — the `**` between name and comma
defeated the regex, so **22 of 47** beats leaked (vs 5 in run 1) and the feedback loop
re-amplified the style it could no longer strip. The regex was broadened to allow bold around
the name *and* a comma / em-dash / en-dash / colon separator, **anchored to a sentence
boundary** (start, `.`/`!`/`?`, or newline) before the name so a mid-sentence name ("Do you
trust Sage, after that?") is not over-stripped — which also closed a latent over-strip the
comma-only form already had. Strips 17/22 of the run-2 leaks cleanly; the residual are
name-less party-wide prompts (correctly kept) plus a rare em-dash-*preceded* / round-banner
shape left untouched to avoid dangling punctuation.

**Consequences.** Cleans the stored/returned narration and breaks the feedback loop — the
high-value effect, since the model stops being trained on its own prompts. This sits in the
same leak-screen family as Decisions 5 & 6. **Accepted residual (mirrors Decision 6):** the
streaming combat paths have already emitted live by the time the strip runs, so the same-turn
on-screen duplicate is only mitigated *indirectly* (the loop stops teaching the pattern); a
fully synchronous fix would require handling it in `_NarrationGate`. Hard boundary on the
helper itself, unit-tested (`tests/test_agent.py::test_strip_turn_prompt_*`); engine numbers
are untouched. Commits `dfed0c1` (original), `b297f08` (broadened).
