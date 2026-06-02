"""Tests for tools.dispatch behaviour. No API calls required."""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import tools
from src.dm_agent import (
    DMAgent, _extract_narration, _sanitize_narration, _screen_narration_text,
    _DUMP_SENTINELS,
)
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


def test_get_state_hides_hidden_npc_dcs():
    """get_state must not hand the model the hidden challenge DCs — the stealth DC
    (alertness_dc) the README redacts, nor its social twin disposition_dc. Other NPC
    fields (hp, hostile, ac) still surface. The live state keeps both (used by the engine)."""
    state = GameState(location="X")
    state.npcs["guard"] = NPC(name="Guard", hostile=True, disposition_dc=14, alertness_dc=12)
    entry = tools.dispatch("get_state", {}, state)["state"]["npcs"]["guard"]
    assert "disposition_dc" not in entry
    assert "alertness_dc" not in entry
    assert entry["hostile"] is True and "hp" in entry and "ac" in entry
    # live state is untouched — the engine still owns the numbers
    assert state.npcs["guard"].disposition_dc == 14
    assert state.npcs["guard"].alertness_dc == 12


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


def test_extract_narration_passes_brace_first_dump_sanitize_is_backstop():
    """Layering contract: _extract_narration / _screen_narration_text only suppress a
    *bracket-first* dump. A brace-first dump (no [Current state] header) passes through
    _extract_narration unchanged — by design, the take_turn-level _sanitize_narration is
    the backstop that catches it. Pins the two-function split so a refactor that stored
    _extract_narration's output directly (skipping _sanitize_narration) surfaces here."""
    brace_first = '{"location": "Dungeon", "party": {"Wisp": {"hp": "8/8"}}}'
    assert _extract_narration(_make_text_blocks(brace_first)) == brace_first  # NOT cleaned
    assert _sanitize_narration(brace_first) == ""                              # backstop cleans it


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


# --- dump-anywhere: a dump is dropped wherever it sits, not assumed dump-first ---

def test_sanitize_drops_trailing_dump_keeps_prose():
    """prose-THEN-dump: the leading prose survives and the trailing dump is dropped.
    (Previously _sanitize_narration assumed dump-first and returned the dump here.)"""
    text = 'The relic gleams in the torchlight.\n\n[Current state]\n{"location": "Vault"}'
    assert _sanitize_narration(text) == "The relic gleams in the torchlight."


def test_sanitize_drops_dump_between_prose():
    text = 'Wisp steps forward.\n\n[Current state]\n{"party": {}}\n\nThe door groans open.'
    assert _sanitize_narration(text) == "Wisp steps forward.\n\nThe door groans open."


def test_sanitize_drops_multiple_dump_paragraphs():
    text = ('[Current state]\n{"location": "A"}\n\n'
            'Grik lunges.\n\n'
            '[Engine: Narl attacked Hero: hit]\n\n'
            'Steel rings on steel.')
    assert _sanitize_narration(text) == "Grik lunges.\n\nSteel rings on steel."


def test_sanitize_all_dump_paragraphs_returns_empty():
    text = '[Current state]\n{"location": "A"}\n\n"party": {"Hero": {}}'
    assert _sanitize_narration(text) == ""


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
    """Regression: a state dump must never reach state.narrative, state.transcript,
    or the take_turn return value.

    Since narration is merged into the tool loop's terminating turn (out of combat),
    the leak guards are now load-bearing on that response. Reproduces:
    - tool-use first response: [TextBlock(state_dump), ToolUseBlock(get_state)]
    - terminating turn: the model leaks a state dump ahead of its prose — _extract_narration
      / _sanitize_narration must strip it before it reaches storage.

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

    # --- terminating turn (captured as narration) — model leaks a state dump ahead
    # of the prose. The guards must strip it before it reaches storage. ---
    leak_block = MagicMock()
    leak_block.type = "text"
    leak_block.text = '[Current state]\n{"location": "Dungeon"}\n\nGrik lunges at Wisp.'

    resp_terminal = MagicMock()
    resp_terminal.stop_reason = "end_turn"
    resp_terminal.content = [leak_block]
    resp_terminal.usage = usage

    client = MagicMock()
    client.messages.create.side_effect = [resp_tool_use, resp_terminal]

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


def test_take_turn_backstops_a_brace_first_dump_extract_misses():
    """End-to-end layering: a BRACE-first dump in the terminating turn slips
    _extract_narration (bracket-only, so it returns the dump verbatim) but is removed by
    take_turn's _sanitize_narration before it reaches the return value, state.narrative,
    or state.transcript. Distinct from test_narration_leak_regression, whose leak is
    bracket-first and so caught one layer earlier — this one exercises the backstop."""
    BRACE_DUMP = '{"location": "Dungeon", "party": {"Wisp": {"hp": "8/8"}}}\n\nGrik lunges at Wisp.'
    # sanity: the dump is exactly the kind _extract_narration does NOT clean
    assert _extract_narration(_make_text_blocks(BRACE_DUMP)) == BRACE_DUMP

    tool_use_block = MagicMock()
    tool_use_block.type = "tool_use"
    tool_use_block.id = "tu_1"
    tool_use_block.name = "get_state"
    tool_use_block.input = {}
    usage = _make_mock_usage()
    resp_tool_use = MagicMock()
    resp_tool_use.stop_reason = "tool_use"
    resp_tool_use.content = [tool_use_block]
    resp_tool_use.usage = usage

    leak_block = MagicMock()
    leak_block.type = "text"
    leak_block.text = BRACE_DUMP
    resp_terminal = MagicMock()
    resp_terminal.stop_reason = "end_turn"
    resp_terminal.content = [leak_block]
    resp_terminal.usage = usage

    client = MagicMock()
    client.messages.create.side_effect = [resp_tool_use, resp_terminal]

    state = GameState(
        party={"wisp": Character(name="Wisp", hp=8, max_hp=8)},
        npcs={"grik": NPC(name="Grik", hp=10, max_hp=10, hostile=True)},
    )
    agent = DMAgent(state, client=client)
    result = agent.take_turn("Wisp casts magic missile at Grik.")

    # the dump is gone end-to-end; the legit prose survives
    assert result == "Grik lunges at Wisp."
    for blob in (result, state.narrative[-1]["text"], state.transcript[-1]["text"]):
        assert '"location"' not in blob and '"party"' not in blob
        assert "Grik lunges at Wisp." in blob


def test_execute_capture_narration_keeps_terminal_prose():
    """With capture_narration=True, the tool loop's terminating turn IS the
    narration: its text is returned (leak-screened) and NOT scrubbed from messages."""
    state = _make_state()

    tool_use_block = MagicMock()
    tool_use_block.type = "tool_use"
    tool_use_block.id = "tu_1"
    tool_use_block.name = "get_state"
    tool_use_block.input = {}

    usage = _make_mock_usage()
    resp1 = MagicMock()
    resp1.stop_reason = "tool_use"
    resp1.content = [tool_use_block]
    resp1.usage = usage

    narr_block = MagicMock()
    narr_block.type = "text"
    narr_block.text = "The door grinds open onto darkness."
    resp2 = MagicMock()
    resp2.stop_reason = "end_turn"
    resp2.content = [narr_block]
    resp2.usage = usage

    client = MagicMock()
    client.messages.create.side_effect = [resp1, resp2]

    agent = DMAgent(state, client=client)
    narration = agent._execute("[Tool-use phase] test", capture_narration=True)

    assert narration == "The door grinds open onto darkness."
    # The terminal prose is kept in the conversation (not scrubbed).
    assert any(
        msg["role"] == "assistant"
        and isinstance(msg["content"], list)
        and any(getattr(b, "type", None) == "text" for b in msg["content"])
        for msg in agent.messages
    )


# ---------------------------------------------------------------------------
# Streaming narration (mocked client.messages.stream, no API)
# ---------------------------------------------------------------------------

class _FakeStream:
    """Minimal stand-in for client.messages.stream(...)'s context manager."""

    def __init__(self, deltas, final_text):
        self._deltas = deltas
        self._final_text = final_text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        return iter(self._deltas)

    def get_final_message(self):
        block = MagicMock()
        block.type = "text"
        block.text = self._final_text
        msg = MagicMock()
        msg.content = [block]
        msg.usage = _make_mock_usage()
        return msg


def test_narration_streams_prose_to_sink():
    """With on_narration_delta set, _narration_call streams deltas live and returns
    the same assembled prose."""
    state = _make_state()
    client = MagicMock()
    client.messages.stream.return_value = _FakeStream(
        ["Three bolts ", "of force ", "slam into Grik."], "Three bolts of force slam into Grik."
    )
    agent = DMAgent(state, client=client)
    chunks: list[str] = []
    agent.on_narration_delta = chunks.append
    agent.messages = [{"role": "user", "content": "narrate"}]

    out = agent._narration_call(max_tokens=256, phase="narrating_turn_1beats")

    assert out == "Three bolts of force slam into Grik."
    assert "".join(chunks) == "Three bolts of force slam into Grik."
    # Streaming path was used, not the buffered create() path.
    client.messages.create.assert_not_called()


def test_narration_stream_gate_suppresses_leading_dump():
    """A leading state dump must never reach the live sink — the gate holds the
    opening, and the leak screen suppresses it (on-screen == stored == "")."""
    state = _make_state()
    client = MagicMock()
    dump = '[Current state]\n{"location": "Dungeon", "party": {}}'
    client.messages.stream.return_value = _FakeStream(
        ['[Current state]\n', '{"location": "Dungeon", ', '"party": {}}'], dump
    )
    agent = DMAgent(state, client=client)
    chunks: list[str] = []
    agent.on_narration_delta = chunks.append
    agent.messages = [{"role": "user", "content": "narrate"}]

    out = agent._narration_call(max_tokens=256, phase="narrating_epilogue")

    assert out == ""                 # leak screen suppressed the stored text
    assert "".join(chunks) == ""     # the dump never reached the sink


# ---------------------------------------------------------------------------
# Leak screens vs. the REAL snapshot shape — ties _DUMP_SENTINELS to reality
# (the other screen tests use a simplified compact dump; these use the actual
# json.dumps(indent=2) [Current state] block the agent injects each turn, so a
# snapshot key rename that drifts the denylist is caught here).
# ---------------------------------------------------------------------------

def _realistic_snapshot() -> str:
    """The actual [Current state] JSON the agent injects each turn — multi-line,
    indented, brace-first — for a representative state including a redacted password."""
    gs = GameState(location="The Vault", current_scene="vault")
    gs.scenes = {"vault": {"location": "The Vault", "exits": {}, "loot": ["relic"]}}
    gs.party["wisp"] = Character(name="Wisp", hp=8, max_hp=16, spell_slots={1: 1})
    gs.npcs["grik"] = NPC(name="Grik", hp=10, max_hp=10, hostile=True)
    gs.quest_flags = {"iron_door_password": "ashfall"}
    return DMAgent(gs, client=object())._state_snapshot()


def test_real_snapshot_omits_secret_and_matches_a_sentinel():
    """Canary: (1) the password is redacted out of the snapshot (the real first-line
    defense — it never reaches context), and (2) some _DUMP_SENTINEL still matches a
    line of the actual snapshot, so a verbatim regurgitation stays detectable. Renaming
    a surfaced top-level key (e.g. 'party') without updating _DUMP_SENTINELS breaks
    detection and fails this test."""
    snap = _realistic_snapshot()
    assert "ashfall" not in snap
    assert any(line.lstrip().startswith(s) for line in snap.splitlines() for s in _DUMP_SENTINELS)


def test_sanitize_trims_real_headed_snapshot_dump():
    snap = _realistic_snapshot()
    leak = f"[Current state]\n{snap}\n\nThe relic gleams in the torchlight."
    assert _sanitize_narration(leak) == "The relic gleams in the torchlight."


def test_sanitize_trims_real_brace_first_snapshot_dump():
    """Even with the [Current state] header dropped, the indented brace-first snapshot
    is still caught (via the 'party:' sentinel on a later line)."""
    snap = _realistic_snapshot()
    leak = f"{snap}\n\nThe relic gleams in the torchlight."
    assert _sanitize_narration(leak) == "The relic gleams in the torchlight."


def test_sanitize_drops_trailing_real_snapshot_keeps_prose():
    """The realistic worst case: legit prose FOLLOWED by a verbatim real snapshot.
    The prose must survive and the whole multi-line snapshot paragraph be dropped."""
    snap = _realistic_snapshot()
    leak = f"The relic gleams in the torchlight.\n\n[Current state]\n{snap}"
    assert _sanitize_narration(leak) == "The relic gleams in the torchlight."


def test_screen_suppresses_real_headed_snapshot_dump():
    snap = _realistic_snapshot()
    assert _extract_narration(_make_text_blocks(f"[Current state]\n{snap}")) == ""
    assert _screen_narration_text(f"[Current state]\n{snap}") == ""


def test_stream_gate_suppresses_real_snapshot_dump():
    """Streaming path: the real-snapshot dump is held and the screen suppresses it —
    nothing reaches the live sink or the stored value."""
    snap = _realistic_snapshot()
    dump = f"[Current state]\n{snap}"
    client = MagicMock()
    client.messages.stream.return_value = _FakeStream([dump], dump)
    agent = DMAgent(_make_state(), client=client)
    chunks: list[str] = []
    agent.on_narration_delta = chunks.append
    agent.messages = [{"role": "user", "content": "narrate"}]
    out = agent._narration_call(max_tokens=256, phase="narrating_epilogue")
    assert out == ""
    assert "".join(chunks) == ""


# ---------------------------------------------------------------------------
# get_state snapshot surfacing (moved from test_rules.py)
# ---------------------------------------------------------------------------

def test_get_state_surfaces_available_scenes():
    """get_state returns the current scene's exits, not the global scene list."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scene_loot_quest_item.json")
    gs = GameState.load(path)
    res = tools.dispatch("get_state", {}, gs)
    assert res["ok"] is True
    state_dict = res["state"]
    assert "exits" in state_dict
    assert state_dict["exits"] == {"the passage descending deeper into the barrow": "ember_chamber"}
    assert "available_scenes" not in state_dict
    assert "scenes" not in state_dict   # omitted to keep context lean


def test_get_state_omits_unbounded_history():
    """get_state must not echo transcript/narrative/log — they grow every turn and the
    model never acts on them, so re-injecting them only bloats context and latency.
    The live numbers the model DOES need (HP, slots, flags) must still be present."""
    gs = GameState(location="Hall")
    gs.party["aldric"] = Character(name="Aldric", max_hp=24, hp=20, spell_slots={1: 2})
    gs.transcript = [{"kind": "player", "text": "x"}, {"kind": "dm", "text": "y"}]
    gs.narrative = [{"turn": 1, "text": "y"}]
    gs.log = ["[turn 1] something happened"]
    gs.quest_flags = {"door_opened": True}

    state_dict = tools.dispatch("get_state", {}, gs)["state"]

    assert "transcript" not in state_dict
    assert "narrative" not in state_dict
    assert "log" not in state_dict
    # The numbers the model acts on survive the trim.
    assert state_dict["party"]["aldric"]["hp"] == 20
    assert state_dict["party"]["aldric"]["spell_slots"] == {1: 2}
    assert state_dict["quest_flags"] == {"door_opened": True}


def test_get_state_history_trim_does_not_touch_saves():
    """The trim is model-facing only — to_dict (the save path) keeps full history."""
    gs = GameState(location="Hall")
    gs.party["aldric"] = Character(name="Aldric")
    gs.transcript = [{"kind": "player", "text": "x"}]
    gs.narrative = [{"turn": 1, "text": "y"}]
    gs.log = ["[turn 1] z"]
    tools.dispatch("get_state", {}, gs)  # must not mutate state
    saved = gs.to_dict()
    assert saved["transcript"] == [{"kind": "player", "text": "x"}]
    assert saved["narrative"] == [{"turn": 1, "text": "y"}]
    assert saved["log"] == ["[turn 1] z"]


def test_get_state_surfaces_current_exits():
    """get_state returns the current scene's exits dict, not a global scene list."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scene_loot_quest_item.json")
    gs = GameState.load(path)
    assert gs.current_scene == "barrow_entrance"
    res = tools.dispatch("get_state", {}, gs)
    assert res["ok"] is True
    state_dict = res["state"]
    # exits contains the current scene's exits (label → target), not the full scene list
    assert "exits" in state_dict
    assert state_dict["exits"] == {"the passage descending deeper into the barrow": "ember_chamber"}
    assert "scenes" not in state_dict
    assert "available_scenes" not in state_dict


# ---------------------------------------------------------------------------
# quest-flag dispatch: normalization, validation, redaction (moved from test_rules.py)
# ---------------------------------------------------------------------------

# --- set_quest_flag validation -----------------------------------------------

def _flag_state():
    gs = GameState(location="Test")
    return gs


def test_set_flag_normalizes_spaces_and_case():
    gs = _flag_state()
    res = tools.dispatch("set_quest_flag", {"flag": "Met The Oracle", "value": True}, gs)
    assert res["ok"] is True
    assert res["flag"] == "met_the_oracle"
    assert "met_the_oracle" in gs.quest_flags


def test_set_flag_normalizes_hyphens():
    gs = _flag_state()
    res = tools.dispatch("set_quest_flag", {"flag": "hero-reborn", "value": True}, gs)
    assert res["ok"] is True
    assert res["flag"] == "hero_reborn"


def test_set_flag_strips_illegal_chars():
    """Chars outside [a-z0-9_] are removed; the remainder is stored."""
    gs = _flag_state()
    res = tools.dispatch("set_quest_flag", {"flag": "door.opened!", "value": True}, gs)
    assert res["ok"] is True
    assert res["flag"] == "dooropened"


def test_set_flag_rejects_empty_after_normalization():
    """A key that normalizes to empty must return ok=False, reason bad_flag_key,
    and must not write anything to quest_flags."""
    for bad in ("", "   ", "!!!", "..."):
        gs = _flag_state()
        res = tools.dispatch("set_quest_flag", {"flag": bad, "value": True}, gs)
        assert res["ok"] is False, f"expected rejection for {bad!r}"
        assert res["reason"] == "bad_flag_key"
        assert gs.quest_flags == {}


def test_set_flag_rejects_reserved_keys():
    """Engine-owned keys must be rejected with reason reserved_flag_key."""
    for key in ("hp", "max_hp", "ac", "spell_slots", "damage", "initiative"):
        gs = _flag_state()
        res = tools.dispatch("set_quest_flag", {"flag": key, "value": True}, gs)
        assert res["ok"] is False, f"expected reserved rejection for {key!r}"
        assert res["reason"] == "reserved_flag_key"
        assert gs.quest_flags == {}


def test_set_flag_reserved_key_checked_after_normalization():
    """'HP' and 'Max-HP' normalize to reserved keys and must also be rejected."""
    for raw in ("HP", "Max-HP", "SPELL_SLOTS"):
        gs = _flag_state()
        res = tools.dispatch("set_quest_flag", {"flag": raw, "value": True}, gs)
        assert res["ok"] is False, f"expected reserved rejection for {raw!r}"
        assert res["reason"] == "reserved_flag_key"


def test_set_flag_accepts_all_primitive_value_types():
    """bool, str, int, float, and None must all be accepted."""
    primitives = [True, False, "slain", 42, 3.14, None]
    for val in primitives:
        gs = _flag_state()
        res = tools.dispatch("set_quest_flag", {"flag": "story_event", "value": val}, gs)
        assert res["ok"] is True, f"primitive {val!r} rejected unexpectedly"
        assert gs.quest_flags["story_event"] == val


def test_set_flag_rejects_non_primitive_value():
    """Lists and dicts are not JSON primitives and must be rejected."""
    for bad_val in ([], {}, [1, 2], {"a": 1}):
        gs = _flag_state()
        res = tools.dispatch("set_quest_flag", {"flag": "event", "value": bad_val}, gs)
        assert res["ok"] is False, f"expected rejection for value {bad_val!r}"
        assert res["reason"] == "bad_flag_value"
        assert gs.quest_flags == {}


def test_set_flag_default_value_is_true():
    """Omitting value must store True (not raise)."""
    gs = _flag_state()
    res = tools.dispatch("set_quest_flag", {"flag": "door_open"}, gs)
    assert res["ok"] is True
    assert gs.quest_flags["door_open"] is True


def test_set_flag_overwrites_existing():
    """Setting the same key twice keeps only the latest value."""
    gs = _flag_state()
    tools.dispatch("set_quest_flag", {"flag": "stage", "value": 1}, gs)
    tools.dispatch("set_quest_flag", {"flag": "stage", "value": 2}, gs)
    assert gs.quest_flags["stage"] == 2


def test_set_flag_stored_key_is_normalized():
    """The stored key in quest_flags is always the normalized form."""
    gs = _flag_state()
    tools.dispatch("set_quest_flag", {"flag": "Found The Key"}, gs)
    assert "found_the_key" in gs.quest_flags
    assert "Found The Key" not in gs.quest_flags


# --- clear_quest_flag --------------------------------------------------------

def test_clear_flag_removes_existing_key():
    gs = _flag_state()
    tools.dispatch("set_quest_flag", {"flag": "door_open", "value": True}, gs)
    res = tools.dispatch("clear_quest_flag", {"flag": "door_open"}, gs)
    assert res["ok"] is True
    assert res["removed"] is True
    assert res["flag"] == "door_open"
    assert "door_open" not in gs.quest_flags


def test_clear_flag_no_op_on_missing_key():
    gs = _flag_state()
    res = tools.dispatch("clear_quest_flag", {"flag": "nonexistent"}, gs)
    assert res["ok"] is True
    assert res["removed"] is False
    assert gs.quest_flags == {}


def test_clear_flag_normalizes_key():
    """Key normalization must match set_quest_flag so the same flag is targeted."""
    gs = _flag_state()
    tools.dispatch("set_quest_flag", {"flag": "met_the_oracle", "value": True}, gs)
    res = tools.dispatch("clear_quest_flag", {"flag": "Met The Oracle"}, gs)
    assert res["ok"] is True
    assert res["removed"] is True
    assert "met_the_oracle" not in gs.quest_flags


def test_clear_flag_rejects_empty_key():
    gs = _flag_state()
    res = tools.dispatch("clear_quest_flag", {"flag": "!!!"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "bad_flag_key"


def test_clear_flag_missing_key_does_not_crash():
    """No-op on a missing key must not raise and must leave quest_flags unchanged."""
    gs = _flag_state()
    gs.quest_flags["existing"] = True
    res = tools.dispatch("clear_quest_flag", {"flag": "does_not_exist"}, gs)
    assert res["ok"] is True
    assert res["removed"] is False
    assert gs.quest_flags == {"existing": True}  # untouched


# --- quest_flag canonical tests (items 4–5 spec) ----------------------------

def test_set_and_overwrite():
    """set True → ok=True, flag is True; set same key to False → overwrites; exactly one key."""
    gs = _flag_state()
    res1 = tools.dispatch("set_quest_flag", {"flag": "door_unlocked", "value": True}, gs)
    assert res1["ok"] is True
    assert gs.quest_flags["door_unlocked"] is True

    res2 = tools.dispatch("set_quest_flag", {"flag": "door_unlocked", "value": False}, gs)
    assert res2["ok"] is True
    assert gs.quest_flags["door_unlocked"] is False
    assert len(gs.quest_flags) == 1   # overwrite, not append


def test_key_normalization():
    """'Door Unlocked' and 'door-unlocked' both normalize to 'door_unlocked'; only one key stored."""
    gs = _flag_state()
    tools.dispatch("set_quest_flag", {"flag": "Door Unlocked", "value": True}, gs)
    tools.dispatch("set_quest_flag", {"flag": "door-unlocked", "value": True}, gs)
    assert len(gs.quest_flags) == 1
    assert "door_unlocked" in gs.quest_flags


def test_roundtrip_safe():
    """After several valid sets json.dumps(quest_flags) must succeed without raising."""
    import json as _json
    gs = _flag_state()
    for flag, val in [
        ("clue_found", True),
        ("npc_disposition", "friendly"),
        ("secret_level", 3),
        ("completion_ratio", 0.5),
        ("unused_slot", None),
    ]:
        tools.dispatch("set_quest_flag", {"flag": flag, "value": val}, gs)
    assert _json.dumps(gs.quest_flags)  # must not raise


def test_clear():
    """clear removes an existing flag (removed=True); clearing a missing key is a no-op (removed=False)."""
    gs = _flag_state()
    tools.dispatch("set_quest_flag", {"flag": "door_unlocked", "value": True}, gs)

    res_remove = tools.dispatch("clear_quest_flag", {"flag": "door_unlocked"}, gs)
    assert res_remove["ok"] is True
    assert res_remove["removed"] is True
    assert "door_unlocked" not in gs.quest_flags

    res_noop = tools.dispatch("clear_quest_flag", {"flag": "door_unlocked"}, gs)
    assert res_noop["ok"] is True
    assert res_noop["removed"] is False


def test_snapshot_surfaces_flags():
    """_state_snapshot includes quest_flags for the model; print_state (/state) omits them.

    String-valued flags are redacted the same way get_state redacts them — the
    snapshot is injected every turn, so a raw string secret here would leak
    continuously. Boolean/numeric flags pass through so the model can read them.
    """
    import io, contextlib, json as _json
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent
    from src.views import print_state

    gs = GameState(location="Test Hall")
    gs.party["aldric"] = Character(name="Aldric")
    tools.dispatch("set_quest_flag", {"flag": "lever_pulled", "value": True}, gs)
    tools.dispatch("set_quest_flag", {"flag": "oracle_spoke", "value": "warned"}, gs)

    agent = DMAgent(gs, client=MagicMock())
    raw_snap = agent._state_snapshot()
    snap = _json.loads(raw_snap)

    # Boolean flags pass through; string flags are redacted (no secret leak).
    assert snap["quest_flags"]["lever_pulled"] is True
    assert snap["quest_flags"]["oracle_spoke"] == "<redacted>"
    assert "warned" not in raw_snap

    # Player-facing /state must not expose any quest flag (flags may be DM-secrets).
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print_state(gs)
    player_view = buf.getvalue()
    assert "lever_pulled" not in player_view
    assert "oracle_spoke" not in player_view


def test_snapshot_redacts_string_password_flag():
    """A password accidentally stored as a string flag never reaches the per-turn
    snapshot verbatim — regression for the get_state/snapshot redaction parity gap."""
    import json as _json
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(location="Vault")
    gs.party["aldric"] = Character(name="Aldric")
    gs.quest_flags["secret_phrase"] = "ashfall"

    agent = DMAgent(gs, client=MagicMock())
    raw_snap = agent._state_snapshot()
    assert "ashfall" not in raw_snap
    assert _json.loads(raw_snap)["quest_flags"]["secret_phrase"] == "<redacted>"


# --- inspiration dispatch ---------------------------------------------------

def test_award_inspiration_dispatch_grants_to_pc():
    gs = _make_state()
    res = tools.dispatch("award_inspiration", {"character": "Hero"}, gs)
    assert res["ok"] is True
    assert gs.party["Hero"].inspiration == 1


def test_award_inspiration_dispatch_refuses_npc():
    gs = _make_state()
    gs.npcs["snik"] = NPC(name="Snik")
    res = tools.dispatch("award_inspiration", {"character": "Snik"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "not_a_pc"


def test_award_inspiration_dispatch_unknown_character():
    gs = _make_state()
    res = tools.dispatch("award_inspiration", {"character": "Nobody"}, gs)
    assert res["ok"] is False
    assert "unknown" in res["error"].lower()


def test_skill_check_dispatch_spends_inspiration():
    from src import rules
    gs = _make_state()
    gs.party["Hero"].inspiration = 1
    rules.force_rolls([2, 19])  # advantage keeps 19
    res = tools.dispatch("skill_check", {"character": "Hero", "ability": "dex", "dc": 10, "use_inspiration": True}, gs)
    assert res["ok"] is True
    assert res["roll"] == 19
    assert res["inspiration_used"] is True
    assert gs.party["Hero"].inspiration == 0
    assert gs.party["Hero"].inspiration_used is True


def test_saving_throw_dispatch_spends_inspiration():
    from src import rules
    gs = _make_state()
    gs.party["Hero"].inspiration = 1
    rules.force_rolls([16, 1])  # keep 16
    res = tools.dispatch("saving_throw", {"character": "Hero", "ability": "con", "dc": 12, "use_inspiration": True}, gs)
    assert res["ok"] is True
    assert res["roll"] == 16
    assert res["inspiration_used"] is True
    assert gs.party["Hero"].inspiration == 0


def _combat_state(actor_key, actor, npc=None):
    gs = GameState()
    gs.party[actor_key] = actor
    gs.npcs["d"] = npc or NPC(name="D", ac=1, hp=30, max_hp=30, hostile=True)
    gs.combat_order = [actor_key, "d"]
    gs.combat_index = 0
    gs.combat_round = 1
    gs.action_used = False
    return gs


def test_dispatch_attack_passes_advantage_and_tags_log():
    from src import rules
    actor = Character(name="A", attack_bonus=0, inventory=["mace"],
                      ability_modifiers={"str": 0}, proficiency_bonus=2)
    gs = _combat_state("a", actor)
    rules.force_rolls([3, 17, 4])  # 2d20 keep 17
    res = tools.dispatch("attack", {"attacker": "A", "defender": "D", "weapon": "mace",
                                    "advantage": True}, gs)
    assert res["ok"] is True
    assert res["roll_mode"] == "advantage"
    assert res["to_hit_roll"] == 17
    assert any("(advantage)" in e for e in gs.log)


def test_dispatch_add_and_spend_gold_update_balance_and_log():
    gs = _make_state()  # party Hero
    add = tools.dispatch("add_gold", {"character": "Hero", "amount": 30}, gs)
    assert add["ok"] is True and gs.party["Hero"].gold == 30
    spend = tools.dispatch("spend_gold", {"character": "Hero", "amount": 25}, gs)
    assert spend["ok"] is True and gs.party["Hero"].gold == 5
    assert any("gains 30 gp" in e for e in gs.log)
    assert any("spends 25 gp" in e for e in gs.log)


def test_dispatch_spend_gold_refuses_overspend():
    gs = _make_state()
    gs.party["Hero"].gold = 5
    res = tools.dispatch("spend_gold", {"character": "Hero", "amount": 10}, gs)
    assert res["ok"] is False and res["reason"] == "insufficient_gold"
    assert gs.party["Hero"].gold == 5  # unchanged


def test_dispatch_gold_on_npc_is_not_a_pc():
    gs = _make_state()
    gs.npcs["snik"] = NPC(name="Snik", hostile=True)
    res = tools.dispatch("add_gold", {"character": "Snik", "amount": 10}, gs)
    assert res["ok"] is False and res["reason"] == "not_a_pc"


def _shop_state(gold=20):
    gs = _make_state()
    gs.party["Hero"].gold = gold
    gs.npcs["garric"] = NPC(name="Garric", hostile=False, shop={"longsword": 15})
    return gs


def test_dispatch_buy_item_auto_selects_sole_merchant_and_logs():
    gs = _shop_state()
    res = tools.dispatch("buy_item", {"character": "Hero", "item": "longsword"}, gs)
    assert res["ok"] is True and res["price"] == 15
    assert gs.party["Hero"].gold == 5
    assert "longsword" in gs.party["Hero"].inventory
    assert any("buys longsword from Garric" in e for e in gs.log)


def test_dispatch_sell_item_pays_and_logs():
    gs = _shop_state(gold=0)
    gs.party["Hero"].inventory = ["longsword"]
    res = tools.dispatch("sell_item", {"character": "Hero", "item": "longsword"}, gs)
    assert res["ok"] is True and gs.party["Hero"].gold == 7
    assert any("sells longsword to Garric" in e for e in gs.log)


def test_dispatch_buy_item_refused_when_merchant_hostile():
    gs = _shop_state()
    gs.npcs["garric"].hostile = True
    res = tools.dispatch("buy_item", {"character": "Hero", "item": "longsword"}, gs)
    assert res["ok"] is False and res["reason"] == "no_merchant"


def test_dispatch_buy_item_buyer_must_be_pc():
    gs = _shop_state()
    res = tools.dispatch("buy_item", {"character": "Garric", "item": "longsword"}, gs)
    assert res["ok"] is False and res["reason"] == "not_a_pc"


def test_dispatch_buy_item_ambiguous_merchant():
    gs = _shop_state()
    gs.npcs["tilda"] = NPC(name="Tilda", hostile=False, shop={"dagger": 2})
    res = tools.dispatch("buy_item", {"character": "Hero", "item": "longsword"}, gs)
    assert res["ok"] is False and res["reason"] == "ambiguous_merchant"
    assert set(res["candidates"]) == {"Garric", "Tilda"}


def test_dispatch_skill_check_passes_skill_and_tags_log():
    from src import rules
    gs = _make_state()
    hero = gs.party["Hero"]
    hero.ability_modifiers = {"dex": 2}
    hero.proficiency_bonus = 2
    hero.skill_proficiencies = ["stealth"]
    rules.force_rolls([10])
    res = tools.dispatch("skill_check", {"character": "Hero", "ability": "dex", "dc": 10,
                                         "skill": "stealth"}, gs)
    assert res["ok"] is True
    assert res["skill"] == "stealth"
    assert res["proficient"] is True
    assert res["total"] == 14  # 10 + dex 2 + proficiency 2
    assert any("(proficient)" in e for e in gs.log)


def test_dispatch_cast_spell_passes_disadvantage_and_tags_log():
    from src import rules
    actor = Character(name="Wisp", level=1, spells=["chromatic_orb"], spell_slots={1: 1},
                      spellcasting_ability="int", ability_modifiers={"int": 3})
    gs = _combat_state("w", actor, npc=NPC(name="D", ac=25, hp=40, max_hp=40, hostile=True))
    rules.force_rolls([18, 2])  # keep 2 → miss vs AC 25
    res = tools.dispatch("cast_spell", {"caster": "Wisp", "spell_name": "chromatic_orb",
                                        "spell_level": 1, "target": "D", "disadvantage": True}, gs)
    assert res["ok"] is True
    assert res["roll_mode"] == "disadvantage"
    assert res["to_hit_roll"] == 2
    assert res["hit"] is False
    assert any("(disadvantage)" in e for e in gs.log)
