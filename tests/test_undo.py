"""Tests for /undo (state rewind) and per-turn autosave — no real API needed.

/undo snapshots pre-turn state in DMAgent and restores it in place; autosave is
a best-effort REPL wrapper around _do_save. The DMAgent turns here are driven by
a fake client that makes no tool calls (a plain text response), so take_turn runs
its full lifecycle — incrementing turn, recording narrative, pushing an undo
snapshot — without a network hit.
"""

import json
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import main
from src.dm_agent import DMAgent, UNDO_DEPTH
from src.game_state import Character, GameState


def _fake_client():
    """A client whose create() always returns a plain-text (no tool_use) response."""
    block = MagicMock(); block.type = "text"; block.text = "Narration."
    resp = MagicMock(); resp.stop_reason = "end_turn"; resp.content = [block]
    client = MagicMock(); client.messages.create.return_value = resp
    return client


def _simple_agent():
    gs = GameState(location="A quiet room.")
    gs.party["aldric"] = Character(name="Aldric", max_hp=24, hp=24)
    return DMAgent(gs, client=_fake_client()), gs


# --- state.restore (the in-place primitive) ----------------------------------

def test_restore_is_in_place_and_lossless():
    """restore() mutates the SAME object (refs stay valid) and round-trips state."""
    gs = GameState(location="Start")
    gs.party["aldric"] = Character(name="Aldric", max_hp=24, hp=24)
    gs.turn = 0
    snapshot = json.loads(json.dumps(gs.to_dict()))  # isolated, as _push_undo stores it
    identity = id(gs)

    # Mutate everything a turn would touch.
    gs.party["aldric"].hp = 5
    gs.turn = 7
    gs.quest_flags["door_opened"] = True
    gs.narrative.append({"turn": 7, "text": "later beat"})

    gs.restore(snapshot)

    assert id(gs) == identity, "restore must mutate in place, not replace the object"
    assert gs.party["aldric"].hp == 24
    assert gs.turn == 0
    assert gs.quest_flags == {}
    assert gs.narrative == []


# --- undo on the agent --------------------------------------------------------

def test_undo_returns_false_when_nothing_to_undo():
    agent, _ = _simple_agent()
    assert agent.undo() is False


def test_undo_rewinds_a_completed_turn():
    agent, gs = _simple_agent()
    agent.take_turn("look around")

    assert gs.turn == 1
    assert len(gs.narrative) == 1
    assert len(agent.full_trace) == 1
    assert len(agent.narration_history) == 1

    assert agent.undo() is True

    assert gs.turn == 0
    assert gs.narrative == []
    assert agent.full_trace == []
    assert agent.narration_history == []
    # Stack is now empty — a second undo has nothing left.
    assert agent.undo() is False


def test_undo_restores_state_mutated_after_the_snapshot():
    """The snapshot is taken pre-turn; restoring it rolls back any state change
    that happened during (or after) the turn — proven here with an HP hit."""
    agent, gs = _simple_agent()
    agent.take_turn("open the door")  # snapshot captured Aldric at full HP

    gs.party["aldric"].hp = 5  # something downstream wounded him
    agent.undo()

    assert gs.party["aldric"].hp == 24


def test_undo_steps_back_one_turn_at_a_time():
    agent, gs = _simple_agent()
    agent.take_turn("turn one")
    agent.take_turn("turn two")
    assert gs.turn == 2

    assert agent.undo() is True
    assert gs.turn == 1
    assert len(gs.narrative) == 1
    assert len(agent.full_trace) == 1

    assert agent.undo() is True
    assert gs.turn == 0
    assert gs.narrative == []
    assert agent.full_trace == []

    assert agent.undo() is False


def test_undo_stack_is_bounded():
    agent, gs = _simple_agent()
    for i in range(UNDO_DEPTH + 5):
        agent.take_turn(f"turn {i}")
    assert len(agent._undo_stack) == UNDO_DEPTH


# --- autosave wrapper ---------------------------------------------------------

def test_autosave_writes_overwriting_state_only(monkeypatch):
    """_autosave calls _do_save with the autosave name, overwrite on, no sidecar."""
    captured = {}

    def fake_do_save(state, raw, *args, overwrite=False, trace=None, **kwargs):
        captured.update(raw=raw, overwrite=overwrite, trace=trace)
        return ("saved", "saves/autosave.json")

    monkeypatch.setattr(main, "_do_save", fake_do_save)
    gs = GameState(location="X")
    main._autosave(gs)

    assert captured["raw"] == main.AUTOSAVE_NAME
    assert captured["overwrite"] is True
    assert captured["trace"] == []  # state only — no trace sidecar on autosave


def test_autosave_reports_error_without_raising(monkeypatch, capsys):
    monkeypatch.setattr(main, "_do_save", lambda *a, **k: ("error", "disk full"))
    main._autosave(GameState(location="X"))  # must not raise
    assert "autosave failed" in capsys.readouterr().out


def test_autosave_roundtrips_to_disk(tmp_path, monkeypatch):
    """End-to-end: autosave writes a loadable savegame with live state."""
    monkeypatch.setattr(main, "SAVE_DIR", tmp_path)
    gs = GameState(location="The Vault")
    gs.party["aldric"] = Character(name="Aldric", max_hp=24, hp=9)
    gs.turn = 3
    main._do_save(gs, main.AUTOSAVE_NAME, base_dir=tmp_path, overwrite=True, trace=[])

    loaded = GameState.load(str(tmp_path / "autosave.json"))
    assert loaded.turn == 3
    assert loaded.party["aldric"].hp == 9
