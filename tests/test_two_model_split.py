"""Two-model split — tool-selection on the fast model, narration on the quality model.

No API: a mocked client records the `model=` it was called with. These assert the
routing contract (DECISIONS / README two-model split) and the per-call model tagging
that feeds /cost.
"""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import rules, tools
from src.dm_agent import DMAgent, MODEL, FAST_MODEL
from src.game_state import Character, GameState, NPC


def _text_client(text="Narration."):
    """A client whose create() always returns a single plain-text (terminal) response."""
    blk = MagicMock(); blk.type = "text"; blk.text = text
    resp = MagicMock(); resp.stop_reason = "end_turn"; resp.content = [blk]
    client = MagicMock(); client.messages.create.return_value = resp
    return client


def _solo_state():
    gs = GameState(location="X")
    gs.party["aldric"] = Character(name="Aldric")
    return gs


def test_execute_tool_selection_uses_fast_model():
    """capture_narration=False (combat / NPC-fallback loop) runs on the fast model."""
    agent = DMAgent(_solo_state(), client=_text_client())
    agent.messages = []
    agent._execute("do it", capture_narration=False)
    assert agent.client.messages.create.call_args.kwargs["model"] == FAST_MODEL


def test_execute_folded_narration_uses_quality_model():
    """capture_narration=True (out-of-combat, the terminating turn IS the narration)
    stays on the quality model."""
    agent = DMAgent(_solo_state(), client=_text_client())
    agent.messages = []
    agent._execute("do it", capture_narration=True)
    assert agent.client.messages.create.call_args.kwargs["model"] == MODEL


def test_narration_call_uses_quality_model():
    agent = DMAgent(_solo_state(), client=_text_client())
    agent.messages = [{"role": "user", "content": "ctx"}]
    agent._narration_call(50, "narrating_test")
    assert agent.client.messages.create.call_args.kwargs["model"] == MODEL


def test_fast_model_none_disables_split():
    """fast_model=None routes every call to the quality model (split off)."""
    agent = DMAgent(_solo_state(), client=_text_client(), fast_model=None)
    agent.messages = []
    agent._execute("do it", capture_narration=False)
    assert agent.client.messages.create.call_args.kwargs["model"] == MODEL


def test_api_stats_tag_model_per_call():
    """Each api_stats entry records the model that served it."""
    agent = DMAgent(_solo_state(), client=_text_client())
    agent.messages = []
    agent._execute("do it", capture_narration=False)
    assert agent.api_stats[-1]["model"] == FAST_MODEL
    agent.api_stats = []
    agent.messages = [{"role": "user", "content": "ctx"}]
    agent._narration_call(50, "narrating_test")
    assert agent.api_stats[-1]["model"] == MODEL


def test_combat_turn_routes_thinking_fast_and_narration_quality():
    """End-to-end: a combat turn's tool-selection is tagged fast, its narration quality.

    The engine resolves the NPC's attack with no API call, so the only two calls are the
    player-action tool loop (fast) and the unified turn narration (quality)."""
    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100})
    gs.npcs["snik"] = NPC(name="Snik", max_hp=20, hp=20, ability_modifiers={"dex": 0})
    gs.party["wisp"] = Character(name="Wisp", ability_modifiers={"dex": -100})
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik", "wisp"]}, gs)

    agent = DMAgent(gs, client=_text_client())
    gs.action_used = True  # simulate Aldric's action spent (mock makes no tool calls)
    agent.take_turn("Aldric swings at Snik")

    api = agent.full_trace[-1]["api_calls"]
    thinking = [a for a in api if a["phase"] == "thinking"]
    narrating = [a for a in api if a["phase"].startswith("narrating")]
    assert thinking and all(a["model"] == FAST_MODEL for a in thinking)
    assert narrating and all(a["model"] == MODEL for a in narrating)
