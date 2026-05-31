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