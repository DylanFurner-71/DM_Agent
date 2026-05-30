"""The DM agent: a tool-use loop around the Anthropic Messages API.

``take_turn`` sends the player's input plus the running conversation, lets the
model call tools as many times as it needs (rolling dice, resolving attacks,
checking state), feeds each tool result back, and returns the final narration
once the model stops requesting tools.
"""

from __future__ import annotations

import os

from anthropic import Anthropic

from . import tools

# NOTE: confirm the current model string at https://docs.claude.com/en/docs/about-claude/models
# It is intentionally the only place the model is named so it's a one-line change.
MODEL = "claude-sonnet-4-6"
MAX_TOOL_HOPS = 12  # safety cap on tool calls per turn

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

Keep the player's agency central: present situations, then react to what they choose.
"""


class DMAgent:
    def __init__(self, state, client: Anthropic | None = None, model: str = MODEL):
        self.state = state
        self.client = client or Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        self.model = model
        self.messages: list[dict] = []
        self.tool_trace: list[dict] = []  # populated each turn; read by debug mode

    def _scene_preamble(self) -> str:
        party = ", ".join(
            f"{c.name} (HP {c.hp}/{c.max_hp})" for c in self.state.party.values()
        )
        return (
            f"[Location: {self.state.location}]\n"
            f"[Party: {party}]\n"
            f"[Scene: {self.state.scene}]"
        )

    def take_turn(self, player_input: str) -> str:
        """Run one full player turn and return the DM's narration."""
        self.tool_trace = []
        user_content = f"{self._scene_preamble()}\n\nPlayer: {player_input}"
        self.messages.append({"role": "user", "content": user_content})
        self.state.turn += 1

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
                # Final narration: collect any text blocks.
                return "".join(b.text for b in resp.content if b.type == "text").strip()

            # Execute every tool the model requested this hop.
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    result = tools.dispatch(block.name, block.input, self.state)
                    self.tool_trace.append({"name": block.name, "input": block.input, "result": result})
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": _json(result),
                        }
                    )
            self.messages.append({"role": "user", "content": tool_results})

        return "(The DM pauses, overwhelmed by the threads of fate — too many actions in one turn.)"


def _json(obj) -> str:
    import json

    return json.dumps(obj)
