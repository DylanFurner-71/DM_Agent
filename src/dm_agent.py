"""The DM agent: a tool-use loop around the Anthropic Messages API.

``take_turn`` resolves the player's input, then — if combat is active —
automatically advances the initiative order and resolves each NPC turn
(calling the appropriate tools, narrating the outcome) until the pointer
lands back on a player-controlled character. The combined narration is
returned as a single string.
"""

from __future__ import annotations

import os

from anthropic import Anthropic

from . import tools

# NOTE: confirm the current model string at https://docs.claude.com/en/docs/about-claude/models
# It is intentionally the only place the model is named so it's a one-line change.
MODEL = "claude-sonnet-4-6"
MAX_TOOL_HOPS = 12  # safety cap on tool calls per _resolve_turn call

SYSTEM_PROMPT = """\
You are the Dungeon Master for a single-session tabletop RPG. You narrate vividly \
and concisely (2-4 sentences per beat), voice NPCs, and keep the story moving.

HARD RULES — these are not optional:
- You do NOT know any game numbers from memory. For every dice roll, attack, spell, \
HP change, or rules question, you MUST call the matching tool and use its result.
- Never invent a dice result or override a tool's outcome. If `cast_spell` returns \
ok=false because the caster is out of slots, the spell FAILS — narrate the fizzle, \
do not let it succeed anyway.
- When you need exact current numbers (HP, remaining slots, who's present), call \
`get_state` rather than guessing.
- Stay in the fiction. Describe outcomes in-world; don't expose raw tool JSON to the \
player, but DO let the mechanical result drive what happens.

COMBAT FLOW — follow this sequence exactly:
1. STARTING: The moment any hostile action is about to occur and no combat is active, \
call `start_combat` with every participant before resolving any attack or spell. \
Never call `attack` or `cast_spell` offensively before `start_combat` has been called.
2. TURN ORDER: The engine owns initiative and advances the pointer automatically; \
`next_turn` is not available to you as a tool. The preamble shows \
"[Combat: Round N — Name's turn]" so you always know who is active. Only that combatant \
may act: the tools enforce this in code and will return ok=false if you try to act for a \
different combatant. A tool returning ok=false because it is not that actor's turn is a \
turn-order enforcement, not a narrative failure — stop calling tools and narrate what the \
active combatant did accomplish.
3. PLAYER TURN NARRATION: When the player's declared action resolves, you MUST narrate \
the outcome of that specific action — damage dealt, spell effects, hit or miss — before \
ending your response. Never skip the player's result to pre-empt future turns. NPC \
counterattacks are narrated separately in their own prompt; do not narrate them here.
4. NPC TURNS: Decide the NPC's action based on its nature (hostile NPCs attack; \
frightened ones flee), then execute it with `attack`, `cast_spell`, `skill_check`, or \
other tools as appropriate. Narrate the result in 1–3 sentences.
5. ENDING: After any action that might finish the fight, call `get_state` and check \
whether any hostile NPCs are still standing (hp > 0). If none remain, call `end_combat` \
immediately and narrate the conclusion.

Keep the player's agency central: present situations, then react to what they choose.
"""


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

    def _resolve_turn(self, user_content: str) -> str:
        """Append user_content as a user message, run the tool-use loop, return narration.

        Each call gets its own MAX_TOOL_HOPS budget so an NPC turn can't consume
        the cap that was meant for the player's action.
        """
        self.messages.append({"role": "user", "content": user_content})
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
                return "".join(b.text for b in resp.content if b.type == "text").strip()
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
        return "(The DM pauses, overwhelmed by the threads of fate — too many actions in one turn.)"

    def take_turn(self, player_input: str) -> str:
        """Resolve the player's action, then automatically run any following NPC turns.

        Returns the full narration for the player's turn plus all NPC turns that
        elapsed before it became a player's turn again.
        """
        self.tool_trace = []
        self.state.turn += 1

        # --- Player's turn ---
        narrations = [self._resolve_turn(f"{self._scene_preamble()}\n\nPlayer: {player_input}")]

        # --- NPC turns (only while combat is active) ---
        # Cap iterations at len(combat_order) so an all-NPC order can't loop forever.
        if self.state.combat_order and self.state.combat_round > 0:
            # Determine whether the current slot is the player who just acted (advance
            # past them) or an NPC that won initiative and hasn't acted yet (resolve
            # them before advancing).
            current_key = self.state.combat_order[self.state.combat_index]
            player_just_acted = current_key in self.state.party

            for i in range(len(self.state.combat_order)):
                if player_just_acted or i > 0:
                    # Advance to the next combatant.
                    adv = tools.dispatch("next_turn", {}, self.state)
                    self.tool_trace.append({"name": "next_turn", "input": {}, "result": adv})
                    if not adv["ok"]:
                        break
                    active_key = adv["active"]
                    active_name = adv["active_name"]
                    active_round = adv["round"]
                else:
                    # First iteration: NPC holds the current slot — use it directly.
                    all_actors = {**self.state.party, **self.state.npcs}
                    active_key = current_key
                    active_name = all_actors[active_key].name if active_key in all_actors else active_key
                    active_round = self.state.combat_round

                if active_key in self.state.party:
                    # A player character is up — stop and wait for human input.
                    break

                # NPC's turn: give it a fresh MAX_TOOL_HOPS budget.
                npc_prompt = (
                    f"[Combat — Round {active_round}, {active_name}'s turn] "
                    f"Decide {active_name}'s action, execute it with the appropriate "
                    f"tool(s), and narrate the outcome in 1–3 sentences. "
                    f"Respect all tool results: ok=false means the action fails and must be "
                    f"narrated as a failure."
                )
                narrations.append(self._resolve_turn(npc_prompt))

        self.full_trace.append({"turn": self.state.turn, "calls": list(self.tool_trace)})

        # Append an engine-authoritative "whose turn" line so the displayed prompt
        # is always derived from state, never from model prose.  This prevents the
        # mismatch where a model says "Aldric's turn" while the engine pointer is
        # elsewhere — the last line the player reads is always ground truth.
        if self.state.combat_order and self.state.combat_round > 0:
            all_actors = {**self.state.party, **self.state.npcs}
            active_key = self.state.combat_order[self.state.combat_index]
            active_name = all_actors[active_key].name if active_key in all_actors else active_key
            narrations.append(f"[{active_name}'s turn — what do you do?]")

        return "\n\n".join(n for n in narrations if n)


def _json(obj) -> str:
    import json

    return json.dumps(obj)
