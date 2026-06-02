"""Tests for the narrative transcript — no API, no input() needed.

Covers three invariants:
  1. _record_turn appends player-then-dm entries, two per call.
  2. transcript round-trips through to_dict / from_dict unchanged.
  3. _context_from_transcript returns the bounded tail mapped to message roles.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.dm_agent import _record_turn, _context_from_transcript, NARRATION_WINDOW
from src.game_state import Character, GameState


# --- _record_turn ------------------------------------------------------------

def test_record_turn_appends_in_order():
    gs = GameState(location="Test")
    _record_turn(gs, "I attack the goblin", "The sword strikes true.")
    _record_turn(gs, "I heal Aldric", "Aldric glows with divine light.")

    assert len(gs.transcript) == 4
    assert gs.transcript[0] == {"kind": "player", "text": "I attack the goblin"}
    assert gs.transcript[1] == {"kind": "dm",     "text": "The sword strikes true."}
    assert gs.transcript[2] == {"kind": "player", "text": "I heal Aldric"}
    assert gs.transcript[3] == {"kind": "dm",     "text": "Aldric glows with divine light."}


def test_record_turn_grows_by_two_each_call():
    gs = GameState(location="Test")
    for i in range(5):
        _record_turn(gs, f"player {i}", f"dm {i}")
        assert len(gs.transcript) == (i + 1) * 2


def test_record_turn_contents_match():
    gs = GameState(location="Test")
    _record_turn(gs, "look around", "The chamber is silent.")
    assert gs.transcript[0]["text"] == "look around"
    assert gs.transcript[1]["text"] == "The chamber is silent."
    assert gs.transcript[0]["kind"] == "player"
    assert gs.transcript[1]["kind"] == "dm"


# --- transcript round-trip ---------------------------------------------------

def test_transcript_round_trips(tmp_path):
    """to_dict / from_dict must preserve transcript order, kind, and text exactly."""
    gs = GameState(location="Crypt")
    gs.party["aldric"] = Character(name="Aldric")
    gs.transcript = [
        {"kind": "player", "text": "I look around"},
        {"kind": "dm",     "text": "Shadows fill the crypt."},
        {"kind": "player", "text": "I open the door"},
        {"kind": "dm",     "text": "The door creaks open revealing a dark passage."},
    ]

    # Exercise the real file path (not just to_dict/from_dict in memory).
    path = tmp_path / "narrative_save.json"
    gs.save(str(path))
    loaded = GameState.load(str(path))

    assert loaded.transcript == gs.transcript


def test_transcript_round_trips_empty():
    """A state with no transcript entries must load back with an empty list."""
    gs = GameState(location="Start")
    restored = GameState.from_dict(gs.to_dict())
    assert restored.transcript == []


def test_transcript_order_preserved():
    """Entries must come back in the same chronological order they were appended."""
    gs = GameState(location="Test")
    for i in range(6):
        _record_turn(gs, f"player turn {i}", f"dm narration {i}")
    restored = GameState.from_dict(gs.to_dict())
    assert restored.transcript == gs.transcript
    for j, entry in enumerate(restored.transcript):
        turn_idx = j // 2
        if j % 2 == 0:
            assert entry == {"kind": "player", "text": f"player turn {turn_idx}"}
        else:
            assert entry == {"kind": "dm", "text": f"dm narration {turn_idx}"}


# --- _context_from_transcript ------------------------------------------------

def test_context_seed_bounded_tail():
    """bound < N: returns last bound entries; bound > N: returns all; roles correct."""
    transcript = []
    for i in range(5):
        transcript.append({"kind": "player", "text": f"player {i}"})
        transcript.append({"kind": "dm",     "text": f"dm {i}"})
    # 10 total entries.

    # bound=4: last 4 entries
    result = _context_from_transcript(transcript, 4)
    assert len(result) == 4
    for orig, msg in zip(transcript[-4:], result):
        expected_role = "user" if orig["kind"] == "player" else "assistant"
        assert msg["role"] == expected_role
        assert msg["content"] == orig["text"]

    # bound > N: all 10 entries returned
    result_all = _context_from_transcript(transcript, 20)
    assert len(result_all) == 10

    # bound=0: empty
    assert _context_from_transcript(transcript, 0) == []


def test_context_player_maps_to_user():
    msgs = _context_from_transcript([{"kind": "player", "text": "hello"}], 10)
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "hello"


def test_context_dm_maps_to_assistant():
    msgs = _context_from_transcript([{"kind": "dm", "text": "world"}], 10)
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["content"] == "world"


def test_context_bound_matches_narration_window():
    """The bound used in DMAgent.__init__ (NARRATION_WINDOW * 2) must produce
    at most NARRATION_WINDOW pairs, consistent with _build_turn_context."""
    transcript = []
    for i in range(NARRATION_WINDOW + 3):  # more than the window
        transcript.append({"kind": "player", "text": f"p{i}"})
        transcript.append({"kind": "dm",     "text": f"d{i}"})

    msgs = _context_from_transcript(transcript, NARRATION_WINDOW * 2)
    assert len(msgs) == NARRATION_WINDOW * 2  # exactly the window worth of entries


# --- seeding integration (no API) --------------------------------------------

def test_agent_seeds_narration_history_from_transcript():
    """DMAgent.__init__ must seed narration_history from the transcript tail
    so _build_turn_context produces continuity on the first post-resume turn."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric")
    for i in range(NARRATION_WINDOW + 2):
        _record_turn(gs, f"player turn {i}", f"dm narration {i}")

    agent = DMAgent(gs, client=MagicMock())

    # Must have seeded exactly NARRATION_WINDOW pairs (the live-loop cap).
    assert len(agent.narration_history) == NARRATION_WINDOW
    # The seeded pairs must be the TAIL of the transcript (most recent turns).
    last_player = f"player turn {NARRATION_WINDOW + 1}"
    last_dm     = f"dm narration {NARRATION_WINDOW + 1}"
    assert agent.narration_history[-1] == (last_player, last_dm)


def test_agent_empty_transcript_seeds_empty_history():
    """A fresh state (no transcript) must start with empty narration_history."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric")
    agent = DMAgent(gs, client=MagicMock())
    assert agent.narration_history == []


# --- moved from test_rules.py ------------------------------------------------
# --- narrative persistence & launch-mode tests --------------------------------

def test_narrative_persisted():
    """Narrative beats survive to_dict -> from_dict round-trip intact."""
    gs = GameState(location="Dungeon")
    gs.party["hero"] = Character(name="Hero")
    gs.narrative.append({"turn": 1, "text": "The hero slays the goblin."})
    gs.narrative.append({"turn": 2, "text": "The treasure chest is opened."})

    restored = GameState.from_dict(gs.to_dict())
    assert restored.narrative == gs.narrative
    assert restored.narrative[0]["text"] == "The hero slays the goblin."
    assert restored.narrative[1]["text"] == "The treasure chest is opened."
