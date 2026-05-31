"""Tests for answer-gated exits. No API calls required."""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import tools
from src.dm_agent import DMAgent
from src.game_state import Character, GameState


def _make_state(requires_answer="ashfall", requires=None):
    """Build a minimal state with one answer-gated exit."""
    iron_exit: dict = {"to": "iron_chamber", "denied": "The door does not move."}
    if requires_answer is not None:
        iron_exit["requires_answer"] = requires_answer
    if requires is not None:
        iron_exit["requires"] = requires
    return GameState(
        current_scene="entry",
        scenes={
            "entry": {
                "location": "Entry Hall",
                "scene": "A heavy iron door stands before you.",
                "exits": {"iron door": iron_exit},
            },
            "iron_chamber": {
                "location": "Iron Chamber",
                "scene": "A dusty chamber.",
                "exits": {},
            },
        },
        party={"hero": Character(name="Hero")},
    )


# ---------------------------------------------------------------------------
# Gate behaviour
# ---------------------------------------------------------------------------

def test_answer_gate_match():
    gs = _make_state()
    res = tools.dispatch("move_scene", {"scene_key": "iron_chamber", "answer": "ashfall"}, gs)
    assert res["ok"] is True
    assert gs.current_scene == "iron_chamber"


def test_answer_gate_normalization():
    """Leading/trailing whitespace and trailing punctuation are stripped before comparison."""
    gs = _make_state()
    res = tools.dispatch("move_scene", {"scene_key": "iron_chamber", "answer": "  Ashfall. "}, gs)
    assert res["ok"] is True
    assert gs.current_scene == "iron_chamber"


def test_answer_gate_wrong_word():
    gs = _make_state()
    res = tools.dispatch("move_scene", {"scene_key": "iron_chamber", "answer": "ashfell"}, gs)
    assert res["ok"] is False
    assert res.get("reason") == "locked"
    assert gs.current_scene == "entry"  # not moved
    assert "ashfall" not in json.dumps(res)  # password not leaked


def test_answer_gate_no_answer():
    gs = _make_state()
    res = tools.dispatch("move_scene", {"scene_key": "iron_chamber"}, gs)
    assert res["ok"] is False
    assert res.get("reason") == "locked"
    assert gs.current_scene == "entry"
    assert "ashfall" not in json.dumps(res)


def test_answer_gate_denied_text_returned():
    """The exit's denied text is returned on failure."""
    gs = _make_state()
    res = tools.dispatch("move_scene", {"scene_key": "iron_chamber", "answer": "wrong"}, gs)
    assert res["error"] == "The door does not move."


# ---------------------------------------------------------------------------
# Ungated / flag-gated regressions
# ---------------------------------------------------------------------------

def test_ungated_exit_no_answer():
    """A bare exit (no gate) moves without supplying answer."""
    gs = _make_state(requires_answer=None)
    res = tools.dispatch("move_scene", {"scene_key": "iron_chamber"}, gs)
    assert res["ok"] is True


def test_ungated_exit_ignores_answer():
    """A bare exit ignores a supplied answer argument."""
    gs = _make_state(requires_answer=None)
    res = tools.dispatch("move_scene", {"scene_key": "iron_chamber", "answer": "whatever"}, gs)
    assert res["ok"] is True


def test_flag_gate_regression_blocked():
    """Flag-gated exits still block when the flag is absent."""
    gs = GameState(
        current_scene="start",
        scenes={
            "start": {
                "location": "Start",
                "scene": "...",
                "exits": {
                    "gate": {"to": "next", "requires": "key_found", "denied": "The gate is locked."}
                },
            },
            "next": {"location": "Next", "scene": "...", "exits": {}},
        },
        party={"hero": Character(name="Hero")},
    )
    res = tools.dispatch("move_scene", {"scene_key": "next"}, gs)
    assert res["ok"] is False
    assert res.get("reason") == "locked"
    assert res.get("required_flag") == "key_found"


def test_flag_gate_regression_open():
    """Flag-gated exits open when the flag is set."""
    gs = GameState(
        current_scene="start",
        scenes={
            "start": {
                "location": "Start",
                "scene": "...",
                "exits": {
                    "gate": {"to": "next", "requires": "key_found", "denied": "The gate is locked."}
                },
            },
            "next": {"location": "Next", "scene": "...", "exits": {}},
        },
        party={"hero": Character(name="Hero")},
        quest_flags={"key_found": True},
    )
    res = tools.dispatch("move_scene", {"scene_key": "next"}, gs)
    assert res["ok"] is True


def test_declared_exit_regression():
    """A scene_key not in current exits is rejected regardless of answer."""
    gs = _make_state()
    res = tools.dispatch("move_scene", {"scene_key": "nowhere", "answer": "ashfall"}, gs)
    assert res["ok"] is False
    assert "nowhere" in res["error"]


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def test_redaction_state_snapshot():
    """_state_snapshot shows answer_required: true; literal password absent."""
    gs = _make_state()
    agent = DMAgent(gs, client=MagicMock())
    snapshot = agent._state_snapshot()
    assert "answer_required" in snapshot
    assert "ashfall" not in snapshot
    assert "requires_answer" not in snapshot


def test_redaction_get_state():
    """get_state shows answer_required: true; literal password absent."""
    gs = _make_state()
    res = tools.dispatch("get_state", {}, gs)
    assert res["ok"] is True
    exits_blob = json.dumps(res["state"].get("exits", {}))
    assert "answer_required" in exits_blob
    assert "ashfall" not in exits_blob
    assert "requires_answer" not in exits_blob


# ---------------------------------------------------------------------------
# No mutation
# ---------------------------------------------------------------------------

def test_no_mutation_state_snapshot():
    """_state_snapshot must not mutate state.scenes — gate still works after call."""
    gs = _make_state()
    agent = DMAgent(gs, client=MagicMock())
    agent._state_snapshot()
    exit_val = gs.scenes["entry"]["exits"]["iron door"]
    assert exit_val.get("requires_answer") == "ashfall"


def test_no_mutation_get_state():
    """get_state must not mutate state.scenes — gate still works after call."""
    gs = _make_state()
    tools.dispatch("get_state", {}, gs)
    exit_val = gs.scenes["entry"]["exits"]["iron door"]
    assert exit_val.get("requires_answer") == "ashfall"


# ---------------------------------------------------------------------------
# Round-trip persistence
# ---------------------------------------------------------------------------

def test_round_trip_preserves_gate():
    """to_dict keeps requires_answer; from_dict restores it; gate enforces correctly."""
    gs = _make_state()
    loaded = GameState.from_dict(json.loads(json.dumps(gs.to_dict())))

    res = tools.dispatch("move_scene", {"scene_key": "iron_chamber", "answer": "ashfell"}, loaded)
    assert res["ok"] is False

    res = tools.dispatch("move_scene", {"scene_key": "iron_chamber", "answer": "ashfall"}, loaded)
    assert res["ok"] is True
    assert loaded.current_scene == "iron_chamber"
