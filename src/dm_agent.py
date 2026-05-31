"""The DM agent: a tool-use loop around the Anthropic Messages API.

Each action resolved in a turn cycle goes through two separate model calls:
  1. _execute  — tool-use loop; state mutates, no prose emitted.
  2. _narrate  — single text-only call; returns 1-3 sentences of in-world narration.

take_turn runs these pairs for the player's action then for each auto-run NPC action,
then appends an engine-sourced closing prompt addressed to the next active player.
The model never sees more than one action per narration call, so it cannot reorder or skip.
"""

from __future__ import annotations

import os

from anthropic import Anthropic

from . import tools

# NOTE: confirm the current model string at https://docs.anthropic.com/en/docs/about-claude/models
MODEL = "claude-sonnet-4-6"
MAX_TOOL_HOPS = 12  # safety cap on tool calls per _execute call

SYSTEM_PROMPT = """\
You are the Dungeon Master for a single-session tabletop RPG. You narrate vividly \
and concisely, voice NPCs, and keep the story moving.

HARD RULES — these are not optional:
- You do NOT know any game numbers from memory. For every dice roll, attack, spell, \
HP change, or rules question, you MUST call the matching tool and use its result.
- Never invent a dice result or override a tool's outcome. If `cast_spell` returns \
ok=false because the caster is out of slots, the spell FAILS — narrate the fizzle, \
do not let it succeed anyway.
- When an action tool returns ok=false because the action itself is invalid — attacking \
with a weapon the attacker doesn't own, casting a spell the caster doesn't know, or \
casting with no slot of that level — SURFACE the failure in the narration phase in plain \
language and re-prompt that same character, e.g. "Aldric has no dagger — he's carrying a \
mace. What does Aldric do?" The engine keeps that character's turn alive; do NOT treat \
this as a turn change. Never silently substitute a different weapon or spell, and never \
fabricate a success. (This is distinct from a turn-guard rejection — "it is not X's turn" \
— which is the engine enforcing order and does advance the pointer.)
- When you need exact current numbers (HP, remaining slots, who's present), call \
`get_state` rather than guessing.
- Stay in the fiction. Never expose raw tool JSON, internal reasoning, process notes, \
or meta-commentary.

TWO-PHASE PROTOCOL — every action uses two separate prompts:
TOOL-USE PHASE  (prompt contains [Tool-use phase]): call tools to resolve the action. \
Write no prose — your only output in this phase is tool calls.
NARRATION PHASE ("Narrate what just happened..."): write 1-3 sentences of in-world prose \
describing what the most recent action achieved — damage dealt, spell effect, hit or miss, \
movement, etc. No tool calls. No prompts. No "what do you do". No turn banners. \
No meta-commentary. One action per call.

COMBAT FLOW:
1. STARTING: Before the first attack or offensive spell, call `start_combat` with every \
participant. Never call `attack` or `cast_spell` offensively before `start_combat`.
2. TURN ORDER: `next_turn` is not available to you — the engine advances the pointer. \
The preamble shows "[Combat: Round N — Name's turn]" so you always know who is active. \
Only that combatant may act; the tools enforce this and return ok=false if you try to \
act for someone else. A turn-guard ok=false is a HARD STOP: call no further tools in \
this [Tool-use phase] — the engine will advance the pointer and prompt the correct player. \
If start_combat reports an active combatant different from the one the player named, \
stop immediately without attempting the named action.
3. NPC TURNS: In the tool-use phase, decide the NPC's action (hostile NPCs attack; \
frightened ones flee) and execute it with `attack`, `cast_spell`, `skill_check`, etc.
4. ENDING: After any action that might finish the fight, call `get_state` to check \
whether any hostile NPCs remain (hp > 0). If none do, call `end_combat`.
5. POST-COMBAT BEAT: When `end_combat` fires, the engine requests a two-paragraph \
closing beat — (1) the finishing blow and its immediate aftermath; (2) brief stock of \
the party (wounds, spent slots, the body, the sudden silence), then re-orient to the \
surroundings (exits, what lies ahead, any points of interest) and close with ONE open \
exploration prompt to the whole party, e.g. "The passage yawns ahead. What do you do?" \
Never use a "<Name>, what do you do?" combat-turn prompt here.

Keep the player's agency central: present situations, then react to what they choose.
"""

_NARRATE_ONLY = (
    "Narrate what just happened in 1–3 sentences of in-world prose. "
    "No tool calls. No prompts. No 'what do you do'. No meta-commentary."
)


class DMAgent:
    def __init__(self, state, client: Anthropic | None = None, model: str = MODEL):
        self.state = state
        self.client = client or Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        self.model = model
        self.messages: list[dict] = []
        self.tool_trace: list[dict] = []  # tool calls from the last turn; read by debug mode
        self.full_trace: list[dict] = []  # cumulative [{turn, calls}] across all turns; read by /trace

    def _scene_preamble(self) -> str:
        party = ", ".join(
            f"{c.name} (HP {c.hp}/{c.max_hp})" for c in self.state.party.values()
        )
        lines = [
            f"[Location: {self.state.location}]",
            f"[Party: {party}]",
            f"[Scene: {self.state.scene}]",
        ]
        if self.state.combat_round > 0:
            all_actors = {**self.state.party, **self.state.npcs}
            active_key = self.state.combat_order[self.state.combat_index]
            active_name = all_actors[active_key].name if active_key in all_actors else active_key
            lines.append(f"[Combat: Round {self.state.combat_round} — {active_name}'s turn]")
        return "\n".join(lines)

    def _execute(self, prompt: str) -> None:
        """Tool-use phase for one action. Runs the loop; state mutates; no narration."""
        self.messages.append({"role": "user", "content": prompt})
        for _ in range(MAX_TOOL_HOPS):
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=tools.TOOLS,
                messages=self.messages,
            )
            self.messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason != "tool_use":
                break
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    result = tools.dispatch(block.name, block.input, self.state)
                    self.tool_trace.append({"name": block.name, "input": block.input, "result": result})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": _json(result),
                    })
            self.messages.append({"role": "user", "content": tool_results})

    def _narrate(self) -> str:
        """Narration phase: single text-only call; returns 1-3 in-world sentences."""
        self.messages.append({"role": "user", "content": _NARRATE_ONLY})
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=256,
            system=SYSTEM_PROMPT,
            messages=self.messages,  # no tools= → text only
        )
        self.messages.append({"role": "assistant", "content": resp.content})
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    def _narrate_combat_over(self) -> str:
        """Post-combat narration: finishing blow, party stock, exploration prompt."""
        party_summary = "; ".join(
            f"{c.name} HP {c.hp}/{c.max_hp}"
            + (f" [slots: {', '.join(f'L{l}:{n}' for l, n in sorted(c.spell_slots.items()))}]"
               if c.spell_slots else "")
            for c in self.state.party.values()
        )
        prompt = (
            "Combat is over — the fight has just ended. "
            "Write two short paragraphs:\n"
            "1. The finishing blow and its immediate aftermath.\n"
            f"2. Brief stock of the party ({party_summary}) — wounds, spent resources, "
            "the body, the sudden silence. Re-orient to the surroundings: exits, what "
            "lies ahead, any points of interest. Close with one open exploration prompt "
            "to the whole party, e.g. 'The passage yawns ahead into the dark. What do "
            "you do?' — not a combat-turn prompt."
        )
        self.messages.append({"role": "user", "content": prompt})
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=self.messages,
        )
        self.messages.append({"role": "assistant", "content": resp.content})
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    def _narrate_for(self, trace_len: int) -> str:
        """Pick regular or post-combat narration based on calls added since trace_len."""
        if any(c["name"] == "end_combat" for c in self.tool_trace[trace_len:]):
            return self._narrate_combat_over()
        return self._narrate()

    def take_turn(self, player_input: str) -> str:
        """Resolve the player's action, then auto-run any following NPC turns.

        Each action (player then each NPC) is an _execute/_narrate pair, so the model
        sees exactly one action per narration call and cannot reorder or skip.
        Returns all narration beats joined, ending with the engine-sourced player prompt.
        """
        self.tool_trace = []
        self.state.turn += 1
        narrations = []

        # --- Player's action ---
        player_prompt = (
            f"{self._scene_preamble()}\n\n"
            f"Player: {player_input}\n\n"
            f"[Tool-use phase] Call the appropriate tools to resolve this action. "
            f"Write no prose — narration is requested separately."
        )
        trace_len = len(self.tool_trace)
        self._execute(player_prompt)
        narrations.append(self._narrate_for(trace_len))

        # --- NPC turns (only while combat is active) ---
        if self.state.combat_order and self.state.combat_round > 0:
            current_key = self.state.combat_order[self.state.combat_index]
            # advance_first: only true if the active player HAS used their action.
            # action_used=False means start_combat just ran (player hasn't acted yet)
            # or the declared action was turn-guard rejected — in either case the
            # pointer stays where the engine left it; do NOT call next_turn.
            advance_first = self.state.action_used and current_key in self.state.party

            for i in range(len(self.state.combat_order)):
                if advance_first or i > 0:
                    adv = tools.dispatch("next_turn", {}, self.state)
                    self.tool_trace.append({"name": "next_turn", "input": {}, "result": adv})
                    if not adv["ok"]:
                        break
                    active_key = adv["active"]
                    active_name = adv["active_name"]
                    active_round = adv["round"]
                else:
                    all_actors = {**self.state.party, **self.state.npcs}
                    active_key = current_key
                    active_name = all_actors[active_key].name if active_key in all_actors else active_key
                    active_round = self.state.combat_round

                if active_key in self.state.party:
                    break

                npc_exec_prompt = (
                    f"[Combat — Round {active_round}, {active_name}'s turn — Tool-use phase] "
                    f"Decide {active_name}'s action and execute it with the appropriate tool(s). "
                    f"Write no prose — narration is requested separately."
                )
                trace_len = len(self.tool_trace)
                self._execute(npc_exec_prompt)
                narrations.append(self._narrate_for(trace_len))
                if self.state.combat_round == 0:  # end_combat fired; don't advance
                    break

        self.full_trace.append({"turn": self.state.turn, "input": player_input, "calls": list(self.tool_trace)})

        # Engine-sourced closing prompt: only prompt a live (non-downed) combatant.
        if self.state.combat_order and self.state.combat_round > 0:
            all_actors = {**self.state.party, **self.state.npcs}
            active_key = self.state.combat_order[self.state.combat_index]
            actor = all_actors.get(active_key)
            if actor and not actor.is_down:
                narrations.append(f"{actor.name}, what do you do?")

        return "\n\n".join(n for n in narrations if n)


def _json(obj) -> str:
    import json

    return json.dumps(obj)
