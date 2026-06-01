# Brainstorming: making DM Agent more agentic

A menu of directions for deepening the project's *agency*, grounded in the current
codebase. The throughline for all of them: **don't weaken enforcement — expand the
validated action space the agent operates in.** The engine being the un-cheatable
source of truth is exactly what makes it safe to hand the model more autonomy.

**Frame.** Today the agent is **reactive, single-turn, single-agent,
bounded-memory**, operating over a **fully author-placed** world. Each of those
adjectives is a lever.

---

## 1. Horizon: reactive → planning

`take_turn` resolves one player input and stops; no plan persists across turns.

- **Add a planner/scratchpad.** The agent maintains an explicit, structured
  "current intent / open threads" object (separate from `quest_flags`) — e.g. "lure
  the ogre onto the bridge," "the party still owes Snik a favor." Read into context
  each turn, updated via a tool.
- **Why more agentic:** goals that outlive a single exchange — the difference between
  reacting and pursuing.
- **First step:** a `plan` field on `GameState` + `set_intent`/`resolve_intent` tools,
  surfaced in the snapshot like `quest_flags`.

## 2. Population: one agent → adversaries as agents (highest-leverage, lowest-risk)

`resolve_npc_action` in `tools.py` makes every hostile do the same dumb thing: attack
the lowest-HP PC. The least agentic part of the system.

- **Give each NPC a goal + tactics.** Add `motivation`/`tactics` fields (e.g.
  `"focus_casters"`, `"flee_below_25pct"`, `"protect:bandit_captain"`,
  `"call_for_help"`). The engine already has a model-fallback path
  (`resolve_npc_action` returns `None` → the model decides the turn) — lean into it:
  let the model run a brief tactical decision for "smart" NPCs, still executed only
  through `attack`/`cast_spell`/`move`, so enforcement is untouched.
- **Morale & self-preservation:** a wounded goblin *flees* or *surrenders* (reuse
  `influence_npc`/`end_combat` for non-lethal resolutions); a captain rallies allies.
- **Why:** combat becomes adversaries with intent, not a damage-race. Very visible in
  play; composes with the reinforcement/companion systems.
- **Preserves the invariant** — an NPC agent still acts only through validated tools.

## 3. Memory: bounded window → durable, retrievable memory

`NARRATION_WINDOW = 4` is a deliberate cost trade-off (DECISIONS §3) — but the agent
forgets. More agentic systems remember and call back.

- **Running summary:** a cheap second model maintains an "adventure so far" summary
  injected alongside the snapshot — long-range continuity without unbounded context.
- **Episodic/semantic store:** a small retrievable log of notable beats (what an NPC
  promised, a description the player latched onto), queried by a `recall` tool when
  relevant. `quest_flags` is already a primitive fact store — this generalizes it from
  booleans to retrievable episodes.
- **First step:** the summary is the cheap win; the retrieval store is the ambitious one.

## 4. Initiative: player-driven → a world that acts on its own

Nothing happens between player inputs. A more agentic world has its own clock.

- **Off-screen / timed events:** a "world tick" where events fire on conditions —
  reinforcements mobilize if the party dawdles, a prisoner's execution timer, a fire
  spreads. `add_npc` is already flag-gated; this adds *time/condition*-driven triggers,
  not just player-triggered ones.
- **Proactive NPCs:** companions and neutrals pursue their own goals (Snik wanders
  off, a merchant arrives).

## 5. Content: authored → generated-within-guardrails (the biggest leap)

Exits, loot, monsters, and reinforcements are all author-placed precisely so the model
can't fabricate — a hard boundary by design. But the same enforcement machinery can
become a **safety rail that lets the model author safely**.

- **Close the generate→validate loop.** Build the **scenario validator** already on the
  roadmap (`python -m src.validate`), then let a *generator agent* author new
  scenes/encounters/loot — gating everything through the validator before it enters the
  world. The model gets creative latitude; the validator guarantees every exit
  resolves, every template exists, every gate names a real flag.
- **Why this is the most agentic move:** the agent stops being confined to a static map
  and starts *building the world as it goes* — while the engine still refuses anything
  malformed. Directly leverages the project's central insight.
- **First step:** the validator (also catches the deleted-fixture class of bug).

## 6. Quality control: open-loop → reflection / self-critique

The agent never checks its own work or deliberately recovers from a bad tool choice.

- **A critic pass:** after tool selection (or after narration), a lightweight verifier
  checks "did the tool calls match the player's intent? did the prose contradict
  state?" The leak screens (`_extract_narration`/`_sanitize_narration`) are a
  primitive, hard-coded critic — generalize that into a reflective step.
- **Retry-on-failure as a loop, not a dead-end:** when a tool returns `ok=false`, the
  agent could *re-plan* rather than just narrate the failure.

## 7. The director: no meta-goal → goal-directed DM (pacing & difficulty)

A real DM optimizes an objective — tension, fairness, fun. This agent has none.

- **A director/dungeon-master agent** above `DMAgent` that tracks pacing and party
  state and makes choices toward a goal: dynamic difficulty (scale a generated
  encounter to remaining HP/slots), spotlight balance (give a quiet PC a moment),
  tension curves. Combined with #5, the director decides *what to generate next*.

## 8. Multi-agent decomposition

The roadmap's two-model split (cheap tools / quality narration) is the seed. Go
further: a **director** (goals/pacing), a **tactician** (NPC turns, #2), a **narrator**
(prose), a **rules-lawyer** (tool selection + validation), an **author** (#5). Clean
separation, each specialized.

---

## Where I'd actually start

Two complementary picks:

1. **Tactical NPCs (#2)** — the fastest, most visible jump in "agentic feel," slots
   straight into `resolve_npc_action`'s existing model-fallback, and can't compromise
   enforcement. A great first proof that more autonomy ≠ less safety.
2. **Validator → generator loop (#5)** — the most *ambitious* and the truest to the
   project's identity (autonomy made safe by enforcement). Start with the validator
   alone (useful immediately), then let the model author against it.

A cross-cutting enhancer that multiplies both: a **running-summary memory (#3)** so the
agent's plans and the NPCs' grudges actually persist.

### One honest caveat

"More agentic" and "better game" aren't automatically the same — every loop you add
(planning, reflection, multi-agent, memory summarization) costs latency and tokens,
which DECISIONS §3/§5/§6 show you've been fighting hard. Pair any of these with a way to
*measure* it: an agency yardstick (autonomy horizon, self-initiated actions per session,
recovery-from-`ok=false` rate) and the cost it adds.
