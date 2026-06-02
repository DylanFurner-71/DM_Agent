"""Tests for the CLI argument surface and the --seed dice-reproducibility hook.

No API and no interactive loop: the parser is built by _make_parser() so the
argument surface is unit-testable, and the seed contract is exercised directly
against rules.roll (the same RNG the flag fixes at startup).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.main import _make_parser
from src import rules
from src.game_state import Character, NPC, GameState


# --- argument parsing ---------------------------------------------------------

def test_seed_flag_parses_to_int():
    args = _make_parser().parse_args(["--seed", "42"])
    assert args.seed == 42


def test_seed_defaults_to_none():
    args = _make_parser().parse_args([])
    assert args.seed is None


def test_seed_coexists_with_scenario_and_other_flags():
    args = _make_parser().parse_args(["data/x.json", "--seed", "7", "--plain", "--no-hud"])
    assert args.scenario == "data/x.json"
    assert args.seed == 7
    assert args.plain is True
    assert args.no_hud is True


# --- the contract the flag relies on -----------------------------------------

def test_same_seed_reproduces_roll_sequence():
    """The whole point of --seed: identical seed → identical dice sequence."""
    rules.force_rolls([])          # clear any queued forced rolls from other tests
    rules.seed(123)
    first = [rules.roll("1d20").total for _ in range(10)]
    rules.seed(123)
    second = [rules.roll("1d20").total for _ in range(10)]
    assert first == second


def test_different_seeds_diverge():
    rules.force_rolls([])
    rules.seed(1)
    a = [rules.roll("1d20").total for _ in range(10)]
    rules.seed(2)
    b = [rules.roll("1d20").total for _ in range(10)]
    assert a != b   # 20**10 collision odds — effectively never equal


# --- moved from test_rules.py ------------------------------------------------
def test_launch_mode():
    """States with any play history are 'resume'; fresh states are 'new'."""
    from src.main import _launch_mode

    fresh = GameState(location="Start", scene="The adventure begins.")
    assert _launch_mode(fresh) == "new"

    with_narrative = GameState()
    with_narrative.narrative.append({"turn": 1, "text": "Something happened."})
    assert _launch_mode(with_narrative) == "resume"

    with_turn = GameState()
    with_turn.turn = 1
    assert _launch_mode(with_turn) == "resume"

    in_combat = GameState()
    in_combat.combat_round = 2
    assert _launch_mode(in_combat) == "resume"




def test_resume_opening_is_saved_tail():
    """_resume_opening returns last DM beat verbatim; scene text does not bleed in."""
    from src.main import _resume_opening

    scene_text = "The torch-lit hall stretches before you."
    last_beat = "Aldric drives his blade into the troll's knee; it staggers, roaring."

    gs = GameState(scene=scene_text)
    gs.narrative.append({"turn": 1, "text": "First beat."})
    gs.narrative.append({"turn": 2, "text": last_beat})

    opening = _resume_opening(gs)
    assert opening == last_beat
    assert scene_text not in opening




def test_resume_does_not_restart_combat():
    """The resume launch path is pure — it never calls tools or mutates combat state."""
    from src.main import _launch_mode, _resume_opening

    gs = GameState(location="Arena")
    gs.party["hero"] = Character(name="Hero")
    gs.npcs["goblin"] = NPC(name="Goblin", hostile=True)
    gs.narrative.append({"turn": 1, "text": "Combat erupts!"})
    gs.combat_round = 2
    gs.combat_order = ["hero", "goblin"]
    gs.combat_index = 1

    round_before = gs.combat_round
    index_before = gs.combat_index

    assert _launch_mode(gs) == "resume"
    opening = _resume_opening(gs)

    assert gs.combat_round == round_before
    assert gs.combat_index == index_before
    assert gs.combat_order == ["hero", "goblin"]  # unchanged — start_combat not called
    assert opening == "Combat erupts!"




def test_new_uses_scene_intro():
    """Fresh scenario is 'new'; resume opening is empty so scene text is the display."""
    from src.main import _launch_mode, _resume_opening

    scene = "You stand at the entrance to the Iron Keep."
    gs = GameState(scene=scene)

    assert _launch_mode(gs) == "new"
    assert _resume_opening(gs) == ""   # no narrative tail → nothing to replay
    assert gs.scene == scene           # scene text is what main() displays for new games
