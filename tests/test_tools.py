"""Tests for tools.dispatch behaviour. No API calls required."""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import tools
from src.dm_agent import DMAgent, _extract_narration, _sanitize_narration
from src.game_state import Character, GameState, NPC


def _make_state(**quest_flags):
    pc = Character(name="Hero")
    return GameState(party={"Hero": pc}, quest_flags=quest_flags)


def test_get_state_redacts_string_flags():
    state = _make_state(iron_door_password="ashfall", clue_found=True, score=3)
    result = tools.dispatch("get_state", {}, state)
    flags = result["state"]["quest_flags"]
    assert flags["iron_door_password"] == "<redacted>"
    assert flags["clue_found"] is True
    assert flags["score"] == 3


def test_get_state_does_not_mutate_live_flags():
    state = _make_state(secret="password123")
    tools.dispatch("get_state", {}, state)
    assert state.quest_flags["secret"] == "password123"


def test_set_quest_flag_rejects_string_on_password_key():
    state = _make_state()
    res = tools.dispatch("set_quest_flag", {"flag": "iron_door_password", "value": "ashfall"}, state)
    assert res["ok"] is False
    assert res["reason"] == "reserved_answer_key"
    assert "iron_door_password" not in state.quest_flags


def test_set_quest_flag_rejects_string_on_answer_key():
    state = _make_state()
    res = tools.dispatch("set_quest_flag", {"flag": "vault_answer", "value": "swordfish"}, state)
    assert res["ok"] is False
    assert res["reason"] == "reserved_answer_key"


def test_set_quest_flag_bool_on_password_key_succeeds():
    state = _make_state()
    res = tools.dispatch("set_quest_flag", {"flag": "iron_door_password", "value": True}, state)
    assert res["ok"] is True
    assert state.quest_flags["iron_door_password"] is True


def test_set_quest_flag_redacts_string_in_response():
    state = _make_state()
    res = tools.dispatch("set_quest_flag", {"flag": "door_code", "value": "swordfish"}, state)
    assert res["ok"] is True
    assert res["value"] == "<redacted>"
    assert state.quest_flags["door_code"] == "swordfish"


def test_set_quest_flag_non_string_not_redacted_in_response():
    state = _make_state()
    res = tools.dispatch("set_quest_flag", {"flag": "score", "value": 42}, state)
    assert res["value"] == 42
    res = tools.dispatch("set_quest_flag", {"flag": "unlocked", "value": True}, state)
    assert res["value"] is True


def test_execute_scrubs_text_blocks_from_last_assistant_message():
    """Text blocks the model emits during the tool-use phase are removed before narration."""
    state = _make_state()

    # First response: a legitimate tool_use call (get_state)
    tool_use_block = MagicMock()
    tool_use_block.type = "tool_use"
    tool_use_block.id = "tu_1"
    tool_use_block.name = "get_state"
    tool_use_block.input = {}

    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 5
    usage.cache_read_input_tokens = 0
    usage.cache_creation_input_tokens = 0

    resp1 = MagicMock()
    resp1.stop_reason = "tool_use"
    resp1.content = [tool_use_block]
    resp1.usage = usage

    # Second response: end_turn but includes a spurious TextBlock (premature narration)
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "[Current state]\n{...}"

    resp2 = MagicMock()
    resp2.stop_reason = "end_turn"
    resp2.content = [text_block]
    resp2.usage = usage

    client = MagicMock()
    client.messages.create.side_effect = [resp1, resp2]

    agent = DMAgent(state, client=client)
    agent._execute("[Tool-use phase] test prompt")

    # Locate the last tool_result user message
    last_tr_idx = -1
    for i, msg in enumerate(agent.messages):
        if msg["role"] == "user" and isinstance(msg["content"], list):
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in msg["content"]):
                last_tr_idx = i
    assert last_tr_idx >= 0, "Expected at least one tool_result message"

    # No assistant message after that point may contain text blocks
    for msg in agent.messages[last_tr_idx + 1:]:
        if msg["role"] == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                text_items = [b for b in content if hasattr(b, "type") and b.type == "text"]
                assert not text_items, f"Found text blocks in assistant turn after tool_result: {text_items}"


# ---------------------------------------------------------------------------
# _extract_narration
# ---------------------------------------------------------------------------

def _make_text_blocks(*texts):
    """Wrap plain strings as mock text-block objects for _extract_narration."""
    blocks = []
    for t in texts:
        b = MagicMock()
        b.type = "text"
        b.text = t
        blocks.append(b)
    return blocks


def test_extract_narration_suppresses_state_dump():
    content = _make_text_blocks('[Current state]\n{"location": "Entry Hall", "party": {}}')
    assert _extract_narration(content) == ""


def test_extract_narration_suppresses_engine_preamble():
    content = _make_text_blocks('[Engine: Narl attacked Hero: hit, 6 dmg → Hero HP 4/10]{"ok": true}')
    assert _extract_narration(content) == ""


def test_extract_narration_passes_normal_prose():
    content = _make_text_blocks("Three bolts of force slam into Grik.")
    assert _extract_narration(content) == "Three bolts of force slam into Grik."


def test_extract_narration_bracket_without_brace_passes():
    """A line starting with '[' but containing no '{' is not a state dump."""
    content = _make_text_blocks("[Silence falls over the chamber.]")
    assert _extract_narration(content) == "[Silence falls over the chamber.]"


def test_extract_narration_strips_whitespace():
    content = _make_text_blocks("  The goblin falls.  ")
    assert _extract_narration(content) == "The goblin falls."


# ---------------------------------------------------------------------------
# _sanitize_narration
# ---------------------------------------------------------------------------

def test_sanitize_narration_trims_state_dump_prefix():
    text = "[Current state]\n{...truncated json...}\n\nGrik lunges at Wisp with his shortsword."
    assert _sanitize_narration(text) == "Grik lunges at Wisp with his shortsword."


def test_sanitize_narration_trims_engine_preamble():
    text = '[Engine: Narl attacked Hero: hit, 6 dmg]\n\nThe arrow finds its mark.'
    assert _sanitize_narration(text) == "The arrow finds its mark."


def test_sanitize_narration_trims_json_location_line():
    text = '{"location": "Entry Hall", "party": {}}\n\nYou step into the hall.'
    assert _sanitize_narration(text) == "You step into the hall."


def test_sanitize_narration_trims_party_key_line():
    text = '"party": {"Hero": {"hp": "8/10"}}\n\nHero presses forward.'
    assert _sanitize_narration(text) == "Hero presses forward."


def test_sanitize_narration_returns_empty_when_all_dump():
    text = "[Current state]\n{...json...}"
    assert _sanitize_narration(text) == ""


def test_sanitize_narration_passes_clean_prose():
    text = "Three bolts of force slam into Grik."
    assert _sanitize_narration(text) == text


def test_sanitize_narration_no_mutation_on_clean():
    """Clean text is returned as-is — no copy, no strip, no change."""
    text = "The door creaks open."
    assert _sanitize_narration(text) is text


# ---------------------------------------------------------------------------
# End-to-end narration leak regression (mocked client, no API)
# ---------------------------------------------------------------------------

def _make_mock_usage():
    u = MagicMock()
    u.input_tokens = 50
    u.output_tokens = 10
    u.cache_read_input_tokens = 0
    u.cache_creation_input_tokens = 0
    return u


def test_narration_leak_regression():
    """Regression: trace-8 failure mode — state dump mixed into a tool-use response
    must never reach state.narrative, state.transcript, or the take_turn return value.

    Reproduces the exact pattern:
    - tool-use phase first response: [TextBlock(state_dump), ToolUseBlock(get_state)]
    - tool-use phase second response: stop_reason=end_turn (model is done with tools)
    - narration phase response: model echoes state text before actual prose (defense-in-depth)

    Should FAIL if _extract_narration or _sanitize_narration are removed.
    """
    STATE_DUMP = '[Current state]\n{"location": "Dungeon", "party": {"Wisp": {"hp":'

    # --- tool-use phase: first call —
    # Model emits a TextBlock state dump alongside a legitimate ToolUseBlock.
    dump_block = MagicMock()
    dump_block.type = "text"
    dump_block.text = STATE_DUMP

    tool_use_block = MagicMock()
    tool_use_block.type = "tool_use"
    tool_use_block.id = "tu_1"
    tool_use_block.name = "get_state"
    tool_use_block.input = {}

    usage = _make_mock_usage()

    resp_tool_use = MagicMock()
    resp_tool_use.stop_reason = "tool_use"
    resp_tool_use.content = [dump_block, tool_use_block]
    resp_tool_use.usage = usage

    # --- tool-use phase: second call — model ends tool loop ---
    resp_tool_done = MagicMock()
    resp_tool_done.stop_reason = "end_turn"
    resp_tool_done.content = []
    resp_tool_done.usage = usage

    # --- narration phase — model echoes state info before actual prose ---
    # Simulates the observed failure: _extract_narration and _sanitize_narration
    # together block this from reaching storage.
    narr_block = MagicMock()
    narr_block.type = "text"
    narr_block.text = '[Current state]\n{"location": "Dungeon"}\n\nGrik lunges at Wisp.'

    resp_narr = MagicMock()
    resp_narr.stop_reason = "end_turn"
    resp_narr.content = [narr_block]
    resp_narr.usage = usage

    client = MagicMock()
    client.messages.create.side_effect = [resp_tool_use, resp_tool_done, resp_narr]

    state = GameState(
        party={"wisp": Character(name="Wisp", hp=8, max_hp=8)},
        npcs={"grik": NPC(name="Grik", hp=10, max_hp=10, hostile=True)},
    )
    agent = DMAgent(state, client=client)
    result = agent.take_turn("Wisp casts magic missile at Grik.")

    assert "[Current state]" not in result
    assert '{"location"' not in result
    assert "[Current state]" not in state.narrative[-1]["text"]
    assert '{"location"' not in state.narrative[-1]["text"]
    assert "[Current state]" not in state.transcript[-1]["text"]
    assert '{"location"' not in state.transcript[-1]["text"]
